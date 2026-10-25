"""Install/restore transaction model. Simulation only; no USB or native execution.

This is failure-injection coverage for a future real backend, not proof of
camera recovery. Live writes are deliberately unavailable in this module.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path

CP_KEYS = (0x02A40001, 0x02A4000D)
AP_KEYS = (0x02CF1287, 0x02CF1289)


@dataclass(frozen=True)
class Plan:
    body: str
    before_programs: dict
    after_programs: dict
    before_units: dict
    after_units: dict
    before_bindings: dict
    after_bindings: dict

    def validate(self):
        if len(self.body) != 64 or set(self.before_programs) != {'AP', 'CP'} or set(self.after_programs) != {'AP', 'CP'}:
            raise ValueError('Invalid body/program plan')
        for key in ('AP', 'CP'):
            if not self.before_programs[key] or len(self.before_programs[key]) != len(self.after_programs[key]):
                raise ValueError('Program length changed')
        if set(self.before_units) != set(CP_KEYS) or set(self.after_units) != set(CP_KEYS):
            raise ValueError('Unreviewed CP units')
        for key, length in zip(CP_KEYS, (8192, 28672)):
            if len(self.before_units[key]) != length or len(self.after_units[key]) != length:
                raise ValueError('Wrong CP unit length')
            allowed = {at for lo, hi in owned_ranges(key) for at in range(lo, hi)}
            if any(a != b and i not in allowed for i, (a, b) in enumerate(zip(self.before_units[key], self.after_units[key]))):
                raise ValueError('Unrelated calibration bytes changed in plan')
            restored = bytearray(self.after_units[key])
            for lo, hi in owned_ranges(key):
                restored[lo:hi] = self.before_units[key][lo:hi]
            if bytes(restored) != self.before_units[key]:
                raise ValueError('Unit inverse failed')
        if set(self.before_bindings) != set(AP_KEYS) or self.after_bindings != dict(zip(AP_KEYS, (b'\x0a', b'\x0b'))):
            raise ValueError('Unreviewed AP bindings')
        if any(value != b'\0' for value in self.before_bindings.values()):
            raise ValueError('Reserved binding is already occupied')


def owned_ranges(key):
    if key == CP_KEYS[0]:
        return [(0x1200 + p, 0x1201 + p) for p in (55, 56)] + [(0x1250 + b*64, 0x1250 + b*64 + 64) for b in (23, 25)]
    if key == CP_KEYS[1]:
        return [(0x196 + p, 0x197 + p) for p in (55, 56)] + [(0x1E0 + b*768, 0x1E0 + b*768 + 576) for b in (23, 25)]
    raise ValueError('Unreviewed unit')


def merge_owned(current, before, after, ranges, direction):
    if len(current) != len(before) or len(before) != len(after):
        raise ValueError('Unit size differs')
    result = bytearray(current)
    for lo, hi in ranges:
        if current[lo:hi] not in (before[lo:hi], after[lo:hi]):
            raise ValueError('Unknown contents in project-owned range')
        result[lo:hi] = (after if direction == 'install' else before)[lo:hi]
    return bytes(result)


class SimulationDevice:
    """In-memory model. It has no transport or persistent camera write method."""
    def __init__(self, plan, fail=None):
        self.state = {'body': plan.body, 'programs': dict(plan.before_programs),
                      'units': dict(plan.before_units), 'bindings': dict(plan.before_bindings),
                      'unrelated': {'unit00': b'calibration', 'unit02': b'protected', 'user-settings': b'keep'},
                      'wbi_mode': 2, 'wbi_dev': '*wbi4'}
        self.fail = fail
        self.calls = []

    def read(self):
        self.calls.append('read')
        return deepcopy(self.state)

    def apply(self, stage, target):
        self.calls.append(stage)
        if stage in ('CP_PROGRAM', 'AP_PROGRAM'):
            self.state['programs'][stage[:2]] = target
        elif stage == 'CP_PARAMETERS':
            for key, value in target.items():
                self.state['units'][key] = value
                if self.fail == 'PARTIAL_CP_PARAMETERS':
                    raise TimeoutError('Lost acknowledgement after first CP unit')
        elif stage == 'AP_BINDINGS':
            self.state['bindings'].update(target)
        elif stage == 'WBI':
            if self.state['wbi_mode'] != 2 or self.state['wbi_dev'] != '*wbi4':
                raise ValueError('Wrong WBI starting state')
            self.calls.extend(('drop1', 'mode0'))
            self.state['wbi_mode'] = 0
        else:
            raise ValueError('Unknown stage')
        if self.fail == stage:
            raise TimeoutError('Write accepted, acknowledgement lost')
        if self.fail == 'MISMATCH_' + stage and stage.endswith('PROGRAM'):
            self.state['programs'][stage[:2]] = b'corrupted'


class Transaction:
    def __init__(self, plan, device, journal):
        if type(device) is not SimulationDevice:
            raise ValueError('Live camera backends are not accepted by the simulation model')
        plan.validate()
        self.plan, self.device, self.journal = plan, device, Path(journal)
        if self.journal.exists():
            raise ValueError('Existing transaction preserved; inspect before retrying')
        self.events = []
        self.stopped = False

    def record(self, phase, **detail):
        from a7cr_reader import save_json
        self.events.append(dict(phase=phase, **detail))
        save_json(self.journal, {'schema': 'a7cr-simulated-transaction/1', 'simulation_only': True, 'events': self.events})

    def capture(self):
        if self.stopped:
            raise RuntimeError('Unknown prior outcome; no further device commands')
        current = self.device.read()
        if current['body'] != self.plan.body:
            raise ValueError('Wrong body')
        return current

    def stage(self, name, target, expected):
        if self.stopped:
            raise RuntimeError('Unknown outcome')
        self.record('WRITE_INTENT', stage=name)
        try:
            self.device.apply(name, target)
            self.record('WRITE_RETURNED', stage=name)
            got = self.capture()
            if got != expected:
                raise ValueError('Full state readback or preservation mismatch')
            self.record('READBACK_VERIFIED', stage=name)
        except BaseException as error:
            self.stopped = True
            self.record('STOPPED_INSPECTION_REQUIRED', stage=name, error_type=type(error).__name__)
            raise
        return got

    def run(self, direction, *, backups_verified, power_confirmed):
        if direction not in ('install', 'restore') or not backups_verified or not power_confirmed:
            raise ValueError('Verified backups and power confirmation are required')
        try:
            current = self.capture()
            p = self.plan
            combos = [(p.before_programs['AP'], p.before_programs['CP']),
                      (p.before_programs['AP'], p.after_programs['CP']),
                      (p.after_programs['AP'], p.after_programs['CP'])]
            if (current['programs']['AP'], current['programs']['CP']) not in combos:
                raise ValueError('Unknown program state; no writes')
            if direction == 'install' and current['programs'] != p.before_programs:
                raise ValueError('Install was already attempted; do not repeat it')
            if current['wbi_mode'] != 2 or current['wbi_dev'] != '*wbi4':
                raise ValueError('Wrong WBI state; no writes')
            for key in AP_KEYS:
                if current['bindings'][key] not in (p.before_bindings[key], p.after_bindings[key]):
                    raise ValueError('Unknown AP binding; no writes')
            units = {key: merge_owned(current['units'][key], p.before_units[key], p.after_units[key],
                                      owned_ranges(key), direction) for key in CP_KEYS}
            programs = p.after_programs if direction == 'install' else p.before_programs
            bindings = p.after_bindings if direction == 'install' else p.before_bindings
            order = ('CP_PROGRAM', 'CP_PARAMETERS', 'AP_PROGRAM', 'AP_BINDINGS') if direction == 'install' else (
                'AP_BINDINGS', 'AP_PROGRAM', 'CP_PARAMETERS', 'CP_PROGRAM')
            self.record('PREFLIGHT_PASSED', action=direction)
            for stage in order:
                expected = deepcopy(current)
                if stage.endswith('PROGRAM'):
                    key = stage[:2];target = programs[key]
                    if current['programs'][key] == target:
                        continue
                    expected['programs'][key] = target
                elif stage == 'CP_PARAMETERS':
                    target = units
                    if current['units'] == target:
                        continue
                    expected['units'] = dict(target)
                else:
                    target = bindings
                    if current['bindings'] == target:
                        continue
                    expected['bindings'] = dict(target)
                current = self.stage(stage, target, expected)
            expected = deepcopy(current);expected['wbi_mode'] = 0
            current = self.stage('WBI', None, expected)
            self.record('SIMULATED_' + direction.upper() + '_VERIFIED_PENDING_REBOOT')
            return current
        except BaseException as error:
            if not self.stopped:
                self.stopped = True
                self.record('STOPPED_BEFORE_NEXT_WRITE', error_type=type(error).__name__)
            raise
