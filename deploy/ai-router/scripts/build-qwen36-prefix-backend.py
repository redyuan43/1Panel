#!/usr/bin/env python3
"""Build only the validated server-context patch against an audited llama.cpp tree."""
import argparse,hashlib,json,shlex,shutil,subprocess
from pathlib import Path
parser=argparse.ArgumentParser()
parser.add_argument('--source-tree',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--patch',type=Path,default=Path(__file__).resolve().parents[1]/'patches/llama-qwen36-prefix-checkpoints.patch')
a=parser.parse_args();src=a.source_tree/'tools/server/server-context.cpp'
assert hashlib.sha256(src.read_bytes()).hexdigest()=='9b2ca46f07c5efd367fa4801978f7174f94852fc5ea24fe3499a6040d305cb7d','Unvalidated source baseline'
out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
for sub in ['src/tools/server','build','lib']:(out/sub).mkdir(parents=True,exist_ok=True)
patched=out/'src/tools/server/server-context.cpp';shutil.copy2(src,patched)
subprocess.run(['patch','--batch','--forward','-p1','-d',str(out/'src'),'-i',str(a.patch.resolve())],check=True)
assert hashlib.sha256(patched.read_bytes().replace(b'\r\n',b'\n')).hexdigest()=='8d8cb132817011bd1eea9b8b7b2e54c8e273168a47743d2f827fec39430c85d3','Patched source differs from validated source'
original=a.source_tree/'build/tools/server';flags={}
for line in (original/'CMakeFiles/server-context.dir/flags.make').read_text().splitlines():
 if line.startswith('CXX_'):
  key,value=line.split(' = ',1);flags[key]=shlex.split(value)
obj=out/'build/server-context.cpp.o'
subprocess.run(['/usr/bin/c++',*flags['CXX_DEFINES'],*flags['CXX_INCLUDES'],*flags['CXX_FLAGS'],'-c',str(patched),'-o',str(obj)],check=True)
objects=sorted((original/'CMakeFiles/server-context.dir').glob('*.cpp.o'));archive=out/'build/libserver-context.a'
subprocess.run(['/usr/bin/ar','rcs',str(archive),*[str(obj if p.name==obj.name else p) for p in objects]],check=True)
link=shlex.split((original/'CMakeFiles/llama-server-impl.dir/link.txt').read_text());lib=out/'lib/libllama-server-impl.so'
link[link.index('-o')+1]=str(lib);link[link.index('libserver-context.a')]=str(archive)
subprocess.run(link,cwd=original,check=True)
result={'source_sha256':hashlib.sha256(patched.read_bytes()).hexdigest(),'library_sha256':hashlib.sha256(lib.read_bytes()).hexdigest(),'library':str(lib)}
(out/'build.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
