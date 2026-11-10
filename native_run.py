#!/usr/bin/env python3
"""Isolated finite pulse qualification. No publisher entry or live bindings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
os.chdir(Path(__file__).resolve().parent)
os.environ['PYTHONPYCACHEPREFIX'] = tempfile.mkdtemp(prefix='pulse-pycache-')
results = []
for phase, cmd in [('compile', [sys.executable, '-m', 'compileall', '-q', 'src', 'tests']),
                   ('fixtures', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'])]:
    rc = subprocess.call(cmd)
    results.append({'phase': phase, 'returncode': rc})
print('PULSE_QUALIFICATION_RECEIPT ' + json.dumps({'results': results, 'passed': all(x['returncode'] == 0 for x in results)}), flush=True)
while True:
    time.sleep(3600)
