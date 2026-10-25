# Shared Windows SetupAPI reader from the parent A7C II project; see LICENSE.
"""Read present Sony USB bindings directly through Windows SetupAPI."""
import ctypes as C
from ctypes import wintypes as W


def devices():
    setup=C.WinDLL('setupapi',use_last_error=True)
    config=C.WinDLL('cfgmgr32',use_last_error=True)
    class Info(C.Structure):
        _fields_=[('cbSize',W.DWORD),('guid',W.BYTE*16),('devinst',W.DWORD),('reserved',C.c_size_t)]
    setup.SetupDiGetClassDevsW.argtypes=[C.c_void_p,W.LPCWSTR,C.c_void_p,W.DWORD]
    setup.SetupDiGetClassDevsW.restype=C.c_void_p
    setup.SetupDiEnumDeviceInfo.argtypes=[C.c_void_p,W.DWORD,C.POINTER(Info)]
    setup.SetupDiEnumDeviceInfo.restype=W.BOOL
    setup.SetupDiGetDeviceInstanceIdW.argtypes=[C.c_void_p,C.POINTER(Info),W.LPWSTR,W.DWORD,C.POINTER(W.DWORD)]
    setup.SetupDiGetDeviceInstanceIdW.restype=W.BOOL
    setup.SetupDiGetDeviceRegistryPropertyW.argtypes=[C.c_void_p,C.POINTER(Info),W.DWORD,
        C.POINTER(W.DWORD),C.c_void_p,W.DWORD,C.POINTER(W.DWORD)]
    setup.SetupDiGetDeviceRegistryPropertyW.restype=W.BOOL
    setup.SetupDiDestroyDeviceInfoList.argtypes=[C.c_void_p]
    setup.SetupDiDestroyDeviceInfoList.restype=W.BOOL
    config.CM_Get_DevNode_Status.argtypes=[C.POINTER(W.ULONG),C.POINTER(W.ULONG),W.DWORD,W.ULONG]
    config.CM_Get_DevNode_Status.restype=W.ULONG
    handle=setup.SetupDiGetClassDevsW(None,'USB',None,6) # PRESENT | ALLCLASSES
    if handle==C.c_void_p(-1).value:raise C.WinError(C.get_last_error())
    rows=[]
    try:
        index=0
        while True:
            info=Info();info.cbSize=C.sizeof(info)
            if not setup.SetupDiEnumDeviceInfo(handle,index,C.byref(info)):
                error=C.get_last_error()
                if error==259:break
                raise C.WinError(error)
            index+=1
            identifier=C.create_unicode_buffer(512);needed=W.DWORD()
            if not setup.SetupDiGetDeviceInstanceIdW(handle,C.byref(info),identifier,len(identifier),C.byref(needed)):
                raise C.WinError(C.get_last_error())
            if not identifier.value.upper().startswith('USB\\VID_054C&PID_'):continue
            errors={}
            def property(number):
                raw=C.create_string_buffer(16384);kind=W.DWORD();size=W.DWORD()
                if not setup.SetupDiGetDeviceRegistryPropertyW(handle,C.byref(info),number,
                        C.byref(kind),raw,len(raw),C.byref(size)):
                    error=C.get_last_error()
                    if error!=13:errors[str(number)]=error # Missing optional value.
                    return ''
                return raw.raw[:size.value].decode('utf-16-le').rstrip('\0')
            description=property(0);name=property(12) or description
            service=property(4);compatible=property(2)
            flags=W.ULONG();problem=W.ULONG()
            code=config.CM_Get_DevNode_Status(C.byref(flags),C.byref(problem),info.devinst,0)
            row=dict(id=identifier.value,name=name,description=description,service=service,
                compatible_ids=compatible.split('\0') if compatible else [],problem=problem.value,
                status='Unknown' if code else 'Error' if problem.value else 'OK')
            if code:row['status_query_code']=code
            if errors:row['property_errors']=errors
            rows.append(row)
    finally:setup.SetupDiDestroyDeviceInfoList(handle)
    return rows
