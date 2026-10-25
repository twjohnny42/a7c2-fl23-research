"""Private A7CR per-body installation planning. This module never opens USB."""
from pathlib import Path
from copy import deepcopy
import hashlib,json,io,re,uuid
from datetime import datetime,timezone
from a7cr_reader import save_json,backup_sections,CP_UNITS,PROGRAMS
from a7cr_operations import verify_checkpoint
from a7cr_transaction_model import Plan,owned_ranges

CP_KEYS=(0x02A40001,0x02A4000D)
AP_KEYS=(0x02CF1287,0x02CF1289)
WORKERS=('program_check','cp_stage','ap_stage','wbi_check','wbi_stage')


def sha(raw):return hashlib.sha256(raw).hexdigest()


def apply_windows(original,spec):
    """Patch captured bytes, not a factory replacement. No firmware SHA whitelist."""
    if len(original)!=spec['bytes']:raise ValueError('Program size differs')
    result=bytearray(original);end=0
    for window in spec['windows']:
        at,length=window['offset'],window['bytes']
        old,new=bytes.fromhex(window['before_hex']),bytes.fromhex(window['after_hex'])
        if (type(at) is not int or type(length) is not int or at<end or not 0<length<=4096
                or at+length>len(original) or len(old)!=length or len(new)!=length
                or original[at:at+length]!=old):
            raise ValueError('Conflicting, occupied or invalid A7CR patch position')
        result[at:at+length]=new;end=at+length
    if not spec['windows'] or bytes(result)==original:raise ValueError('Empty patch plan')
    return bytes(result)


def merge_colors(original,donor):
    """Only FL2/FL3 parameter selectors and previously unreferenced empty banks."""
    result={k:bytearray(original[k]) for k in CP_KEYS}
    for target,bank,source,sourcebank in ((55,23,54,22),(56,25,55,24)):
        for key,selector,base,stride,length,selector_count in (
                (CP_KEYS[0],0x1200,0x1250,64,64,80),
                (CP_KEYS[1],0x196,0x1e0,768,576,74)):
            old,don=original[key],donor[key]
            if len(old)!=CP_UNITS[key] or len(don)!=CP_UNITS[key]:raise ValueError('Wrong CP unit size')
            at=base+bank*stride;src=base+sourcebank*stride
            if (don[selector+source]!=sourcebank or old[selector+target]
                    or bank in old[selector:selector+selector_count] or any(old[at:at+length])):
                raise ValueError('Occupied target bank, selector or unexpected donor')
            result[key][selector+target]=bank
            result[key][at:at+length]=don[src:src+length]
    return {key:bytes(value) for key,value in result.items()}


def donor_units(workspace):
    from pmca.backup import BackupFile
    folder=Path(workspace)/'private/components/ILCE-1M2_4.00'
    manifest=json.loads((folder/'components.private.json').read_text(encoding='utf-8'))
    matches=[i for i in manifest['selected'] if '00_ALLLANG' in i['file']]
    if len(matches)!=1:raise ValueError('Ambiguous donor factory component')
    item=matches[0];path=(folder/item['file']).resolve()
    if not path.is_relative_to(folder.resolve()) or path.is_symlink():raise ValueError('Donor path escapes component folder')
    raw=path.read_bytes()
    if len(raw)!=item['bytes'] or sha(raw)!=item['sha256']:raise ValueError('Donor source changed')
    sections=[]
    for match in re.finditer(b'BK4\0',raw):
        try:props=dict(BackupFile(io.BytesIO(raw[match.start()-12:])).listProperties())
        except Exception:continue
        if all(key in props for key in CP_KEYS):sections.append(props)
    if len(sections)!=1:raise ValueError('Ambiguous donor CP settings')
    return {key:sections[0][key].data for key in CP_KEYS}


def build_workers(root,audit):
    import a7cr_native_build as native
    before=[audit['programs'][k]['source_sha256'] for k in ('AP','CP')]
    after=[audit['programs'][k]['candidate_sha256'] for k in ('AP','CP')]
    sizes=[audit['programs'][k]['bytes'] for k in ('AP','CP')]
    windows=[dict(w,chip=i) for i,k in enumerate(('AP','CP')) for w in audit['programs'][k]['windows']]
    middle=[before[0],after[1]]
    specs={}
    for kind,left,right,selected,readonly in (
            ('program_check',before,after,windows,True),
            ('cp_stage',before,middle,[w for w in windows if w['chip']==1],False),
            ('ap_stage',middle,after,[w for w in windows if w['chip']==0],False)):
        specs[kind]=native.compile_worker(root/kind,kind,native.source('program',readonly),
                    native.manifest_header(left,right,sizes,selected,
                        [dict(w,before_hex=w['before_hex'] if kind=='cp_stage' else w['after_hex'])
                         for w in windows if (kind=='cp_stage' and w['chip']==0) or (kind=='ap_stage' and w['chip']==1)]))
    for kind,readonly in (('wbi_check',True),('wbi_stage',False)):
        specs[kind]=native.compile_worker(root/kind,kind,native.source('wbi',readonly))
    if b'/sys/power/image_drop' in Path(specs['wbi_check']['file']).read_bytes():
        raise ValueError('Check-only worker retains WBI write path')
    return {kind:Path(spec['file']).read_bytes() for kind,spec in specs.items()}


def prepare_job(destination,first,second,offline_workspace,*,worker_builder=build_workers):
    """Verified backups -> exact edits -> fresh parameter merge -> inverse workers."""
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Existing installation job preserved')
    verified=verify_checkpoint(first,second)
    first,second=Path(first).resolve(),Path(second).resolve()
    if first==destination or second==destination or any(p in destination.parents for p in (first,second)):
        raise ValueError('Install workspace must be separate from backups')
    offline_workspace=Path(offline_workspace).resolve()
    # This audit is produced by the pinned source checks and CPU emulation.
    audit=deepcopy(json.loads((offline_workspace/'private/offline/fl23-candidate/audit.private.json').read_text(encoding='utf-8')))
    sections=backup_sections((first/'settings-memory.private.bin').read_bytes())
    if any(sections[0][key].data!=b'\0' for key in AP_KEYS):raise ValueError('Reserved AP binding is occupied')
    programs={};payloads={}
    for kind,filename in (('AP','appFw.so'),('CP','cpapp-b.bin')):
        original=(first/filename).read_bytes()
        candidate=apply_windows(original,audit['programs'][kind])
        payloads['original-'+kind]=original;payloads['candidate-'+kind]=candidate
        audit['programs'][kind].update(source_sha256=sha(original),candidate_sha256=sha(candidate))
        programs[kind]=original
    originals={key:sections[1][key].data for key in CP_KEYS}
    merged=merge_colors(originals,donor_units(offline_workspace))
    Plan(verified['body_binding']['ap_serial_sha256'],programs,
         {k:payloads['candidate-'+k] for k in programs},originals,merged,
         dict.fromkeys(AP_KEYS,b'\0'),dict(zip(AP_KEYS,(b'\x0a',b'\x0b')))).validate()
    for key in CP_UNITS:payloads[f'original-{key:08x}']=sections[1][key].data
    for key,raw in merged.items():payloads[f'candidate-{key:08x}']=raw
    destination.mkdir(parents=True,exist_ok=False)
    save_json(destination/'state.private.json',{'phase':'BUILDING_LOCAL_JOB','camera_accessed':False})
    payloads.update(worker_builder(destination/'native-build',audit))
    if not set(WORKERS)<=set(payloads):raise ValueError('Incomplete native worker set')
    (destination/'payload').mkdir()
    files={}
    for name,raw in payloads.items():
        if name.startswith('original-'):
            filename={'original-AP':'appFw.so','original-CP':'cpapp-b.bin'}.get(name)
            if filename is None:filename='unit-'+name.removeprefix('original-')+'.private.bin'
            files[name]={'storage':'backup2','file':filename,'bytes':len(raw),'sha256':sha(raw)}
            continue
        path=destination/'payload'/(name+'.private.bin')
        from a7cr_install import store_private
        store_private(path,raw)
        if path.read_bytes()!=raw:raise ValueError('Prepared payload readback differs')
        files[name]={'file':path.relative_to(destination).as_posix(),'bytes':len(raw),'sha256':sha(raw)}
    manifest={'schema':'a7cr-fl23-local-installation/2','model':'ILCE-7CR','version':'2.01',
              'body_binding':verified['body_binding'],'audit':audit,'files':files,
              'sources':{'first_backup':str(first),'second_backup':str(second)},
              'camera_preflight_passed':False,'is_sony_signed_update':False,
              'physical_restoration_proven':False}
    save_json(destination/'package.private.json',manifest)
    from a7cr_install import Package
    Package(destination,verify_backups=False)
    save_json(destination/'state.private.json',{'phase':'PREPARED_NOT_INSTALLED','camera_accessed':False})
    return destination
