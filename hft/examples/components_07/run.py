#!/usr/bin/env python3
"""Generate SBE C++ codecs and check versioning against independent wire bytes."""
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent
VERSION = '1.40.2'
SHA256 = 'f2ffd992f69e3e88b674f36a56424704bda89a98e18bd4c73054b0ec6da196a4'
JAVA = os.environ.get('JAVA_BIN') or shutil.which('java')
CXX = os.environ.get('CXX') or shutil.which('c++')
if not JAVA or not CXX:
    raise SystemExit('Set JAVA_BIN to JDK 17+ java and CXX to a C++20 compiler.')
with tempfile.TemporaryDirectory(prefix='hft-sbe-') as tmp:
    work = Path(tmp)
    jar = work / f'sbe-all-{VERSION}.jar'
    urllib.request.urlretrieve(f'https://repo.maven.apache.org/maven2/uk/co/real-logic/sbe-all/{VERSION}/sbe-all-{VERSION}.jar', jar)
    if hashlib.sha256(jar.read_bytes()).hexdigest() != SHA256:
        raise SystemExit('SBE generator checksum mismatch')
    generated = work / 'generated'
    subprocess.run([JAVA, '-Dsbe.target.language=Cpp', '-Dsbe.generate.precedence.checks=true',
                    f'-Dsbe.output.dir={generated}', '-jar', str(jar),
                    str(ROOT/'event-v0.xml'), str(ROOT/'event-v1.xml')], check=True)
    binary = work/'codec_check'
    subprocess.run([CXX, '-std=c++20', '-O1', '-g', '-fsanitize=address,undefined',
                    '-DSBE_ENABLE_PRECEDENCE_CHECKS', f'-I{generated}',
                    str(ROOT/'codec_check.cpp'), '-o', str(binary)], check=True)
    output = subprocess.check_output([str(binary)], text=True)
    print(output, end='')
    # Separate encoder, no generated accessors: exact byte-level oracle.
    golden = (struct.pack('<HHHHI Q q q Q I', 40, 1, 7, 1, 42, 1001, 1234500, 3, 999, 5)
              + struct.pack('<HH q q q q H', 16, 2, 1234500, 3, 1234400, 7, 4) + b'XNAS')
    actual = bytes.fromhex(output.split('v1 hex=',1)[1].strip())
    assert actual == golden, 'Generated codec bytes differ from independent oracle'
    print('independent Python wire oracle=pass')
