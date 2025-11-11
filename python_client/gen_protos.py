import os
import sys
from pathlib import Path

def main():
    here = Path(__file__).parent
    protos_dir = here / 'protos'
    out_dir = here

    # Ensure package dirs exist
    (protos_dir / 'rapeech' / 'asr' / 'v1').mkdir(parents=True, exist_ok=True)

    # Copy proto sources from repo if not already copied
    repo_root = Path(__file__).resolve().parents[1]
    src1 = repo_root / 'Protos' / 'recognizer.proto'
    src2 = repo_root / 'Protos' / 'rapeech' / 'asr' / 'v1' / 'result.proto'
    dst1 = protos_dir / 'recognizer.proto'
    dst2 = protos_dir / 'rapeech' / 'asr' / 'v1' / 'result.proto'
    if src1.exists():
        dst1.write_bytes(src1.read_bytes())
    if src2.exists():
        dst2.parent.mkdir(parents=True, exist_ok=True)
        dst2.write_bytes(src2.read_bytes())

    # Run protoc
    try:
        from grpc_tools import protoc
    except Exception as e:
        print('grpc_tools not installed. Run: pip install -r requirements.txt', file=sys.stderr)
        raise

    rc = protoc.main([
        'protoc',
        f'-I{protos_dir}',
        f'--python_out={out_dir}',
        f'--grpc_python_out={out_dir}',
        str(dst1),
        str(dst2),
    ])
    if rc != 0:
        raise SystemExit(rc)
    print('Protos generated into', out_dir)

if __name__ == '__main__':
    main()

