#!/usr/bin/python3
"""Bounded physical test. Returns fans to Auto and restores original profile."""
import argparse
import json
from pathlib import Path
import time

ROOT = Path('/sys/devices/platform/neo-control')

def read():
    state = json.loads((ROOT / 'status').read_text())
    limits = {}
    for p in Path('/sys/class/powercap').glob('intel-rapl*:0/constraint_*_power_limit_uw'):
        try:
            limits[str(p)] = int(p.read_text())
        except OSError:
            pass
    return {'timestamp': time.time(), 'firmware': state, 'cpu_power_limits_uw': limits}

def write(name, value):
    (ROOT / name).write_text(str(value) + '\n')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--watchdog', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    events = []
    original = read()['firmware']
    def record(label):
        data = read()
        events.append({'step': label, **data})
        print(json.dumps(events[-1]), flush=True)
        return data['firmware']
    try:
        record('baseline')
        # Rejection must leave hardware unchanged.
        try:
            write('fan_control', 'manual 0 101')
            raise AssertionError('Invalid fan request was accepted')
        except OSError:
            pass
        assert read()['firmware']['cpu_mode'] == original['cpu_mode']
        write('fan_control', 'max')
        time.sleep(8)
        state = record('maximum_after_8s')
        assert state['cpu_mode'] == state['gpu_mode'] == 2
        assert state['cpu_rpm'] > 0 and state['gpu_rpm'] > 0
        write('fan_control', 'manual 55 65')
        time.sleep(8)
        state = record('manual_55_65_after_8s')
        assert state['cpu_mode'] == state['gpu_mode'] == 3
        assert (state['cpu_percent'], state['gpu_percent']) == (55, 65)
        if args.watchdog:
            # Deliberately omit heartbeat; kernel must recover without this app.
            time.sleep(43)
            state = record('lease_expired')
            assert state['cpu_mode'] == state['gpu_mode'] == 1
        write('fan_control', 'auto')
        state = record('automatic')
        assert state['cpu_mode'] == state['gpu_mode'] == 1
        for profile in [0, 1, 4, 5, 6]:
            if original['profile_mask'] & (1 << profile):
                write('profile', profile)
                time.sleep(1)
                assert record(f'profile_{profile}')['profile'] == profile
    finally:
        errors = []
        for name, value in [('fan_control', 'auto'), ('profile', original['profile'])]:
            try:
                write(name, value)
            except Exception as e:
                errors.append(str(e))
        record('restored')
        Path(args.output).write_text(json.dumps({'events': events, 'restore_errors': errors}, indent=2))
        if errors:
            raise RuntimeError('Restore errors: ' + '; '.join(errors))

if __name__ == '__main__':
    main()
