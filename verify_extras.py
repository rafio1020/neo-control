#!/usr/bin/python3
"""Readback test for lighting and charge limit; restores original settings."""
import json
from pathlib import Path
import time

ROOT = Path('/sys/devices/platform/neo-control')

def read(key):
    return json.loads((ROOT / key).read_text())

def write(key, value):
    (ROOT / key).write_text(str(value) + '\n')

def lighting_value(state):
    return ' '.join(str(state[k]) for k in ['mode','speed','brightness','direction','red','green','blue'])

def main():
    old = {k: read(k) for k in ['lighting','zones','battery_limit']}
    events = [{'step':'baseline', **old}]
    try:
        write('battery_limit', 1 - old['battery_limit'])
        assert read('battery_limit') == 1 - old['battery_limit']
        events.append({'step':'battery_limit_toggled','value':read('battery_limit')})
        write('battery_limit', old['battery_limit'])
        write('lighting', '0 0 50 0 255 0 0')
        red = read('lighting')
        assert red['mode'] == 0 and red['brightness'] == 50
        assert (red['red'], red['green'], red['blue']) == (255, 0, 0)
        assert read('zones')['colors'] == ['ff0000'] * 4
        events.append({'step':'red_static', 'lighting':red, 'zones':read('zones')})
        print('Keyboard static red at 50% brightness', flush=True)
        time.sleep(6)
        for mode in range(1, 8):
            write('lighting', f'{mode} 4 50 1 180 40 120')
            state = read('lighting')
            assert state['mode'] == mode and state['brightness'] == 50
            assert state['speed'] == 4
            if mode in (3, 4): assert state['direction'] == 1
            if mode not in (2, 3):
                assert (state['red'],state['green'],state['blue']) == (180,40,120)
            events.append({'step':f'effect_{mode}', 'lighting':state})
            time.sleep(1)
        write('zones', 'ff0000 00ff00 0000ff ffffff 50')
        zones = read('zones')
        assert zones['colors'] == ['ff0000','00ff00','0000ff','ffffff']
        events.append({'step':'four_zones','zones':zones})
        print('Keyboard zones red / green / blue / white', flush=True)
        time.sleep(10)
    finally:
        errors = []
        for key,value in [('battery_limit',old['battery_limit']),
                          ('zones',' '.join(old['zones']['colors'])+' '+str(old['zones']['brightness'])),
                          ('lighting',lighting_value(old['lighting'])) if old['lighting']['mode'] != 0 else ('zones',' '.join(old['zones']['colors'])+' '+str(old['zones']['brightness']))]:
            try: write(key,value)
            except OSError as e: errors.append(str(e))
        events.append({'step':'restored', **{k:read(k) for k in old}, 'errors':errors})
        path = Path(__file__).resolve().parent / 'evidence' / 'extras-test.json'
        path.write_text(json.dumps(events,indent=2))
        if errors: raise RuntimeError(errors)

if __name__ == '__main__': main()
