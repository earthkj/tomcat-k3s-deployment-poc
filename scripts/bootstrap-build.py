"""Download official Windows build tools with checksum validation; no global installation."""
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'.cache'
TOOLS=ROOT/'.tools'

def download(url,path):
    if not path.exists():
        temporary=path.with_suffix(path.suffix+'.partial')
        with urllib.request.urlopen(url,timeout=60) as src, temporary.open('wb') as dest:
            while chunk:=src.read(1024*1024): dest.write(chunk)
        temporary.replace(path)

def install(url,checksum_url,algorithm,name,expected=None):
    archive=CACHE/url.rsplit('/',1)[1]
    checksum=CACHE/(archive.name+'.checksum')
    download(url,archive)
    if expected is None:
        download(checksum_url,checksum)
        expected=checksum.read_text().split()[0].lower()
    hasher=hashlib.new(algorithm)
    with archive.open('rb') as f:
        while chunk:=f.read(1024*1024): hasher.update(chunk)
    if hasher.hexdigest()!=expected: raise ValueError(f'{name}: checksum mismatch')
    folder=TOOLS/name
    if not folder.exists():
        with zipfile.ZipFile(archive) as z:
            for entry in z.infolist():
                target=(TOOLS/entry.filename).resolve()
                if not target.is_relative_to(TOOLS.resolve()): raise ValueError('unsafe archive path')
            z.extractall(TOOLS)
    return {'url':url,'checksum':expected,'algorithm':algorithm,'directory':str(folder)}

def main():
    CACHE.mkdir(exist_ok=True); TOOLS.mkdir(exist_ok=True)
    java='https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/OpenJDK21U-jdk_x64_windows_hotspot_21.0.12.1_1.zip'
    maven='https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.9/apache-maven-3.9.9-bin.zip'
    # SHA-256 from the official GitHub Release asset digest (2026-10-15).
    lock={'jdk':install(java,java+'.sha256.txt','sha256','jdk-21.0.12.1+1','f9d6e191ab098c0d416e7d588a24420a8621cd2f4720dab2459b8b7b2d2d8b4e'),
          'maven':install(maven,maven+'.sha512','sha512','apache-maven-3.9.9')}
    (TOOLS/'toolchain.json').write_text(json.dumps(lock,indent=2))
    print('Verified Windows JDK and Maven in .tools')

if __name__=='__main__': main()
