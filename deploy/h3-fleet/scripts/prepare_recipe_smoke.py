from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx


ROOT = Path('/mnt/ivan-ext4-offload/h3-throughput-20260910')
RUNTIME = ROOT / 'a-lowmem-runtime'
SOURCE = ROOT / 'fleet-candidate'
UNIT = 'h3-throughput-smoke-main-r3'
GPU = 'GPU-08c21842-c266-7f7d-6e5d-d494d4c20c4f'


def command(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True).stdout.strip()


def production():
    pid = command('systemctl', 'show', 'h3-fleet.service', '-p', 'MainPID', '--value')
    environment = dict(item.split(b'=', 1) for item in (Path('/proc') / pid / 'environ').read_bytes().split(b'\0') if b'=' in item)
    with httpx.Client(base_url='http://100.96.79.21:8789', trust_env=False,
                      headers={'Authorization': 'Bearer ' + environment[b'H3_ROUTER_KEY'].decode()}, timeout=30) as client:
        state = client.get('/api/router/capacity').raise_for_status().json()
    if state['active'] or state.get('validation_lease') or any(queue['queued_or_running'] for queue in state['queues']):
        raise RuntimeError('production must be idle and unleased before smoke setup')
    return environment


def private_file(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(text)


def start_worker():
    production()
    if command('systemctl', 'show', UNIT, '-p', 'MainPID', '--value') != '0':
        raise RuntimeError('experimental worker already exists; reconcile rather than restart')
    worker = ROOT / 'worker-main'
    for name in ('input', 'output', 'temp', 'user'):
        (worker / name).mkdir(parents=True, exist_ok=True)
    arguments = ['sudo', '-n', 'systemd-run', '--unit=' + UNIT, '--property=Slice=h3-compute.slice',
        '--property=MemoryHigh=42G', '--property=MemoryMax=50G', '--property=RuntimeMaxSec=7200',
        '--property=TimeoutStopSec=60', '--property=LimitCORE=0', '--setenv=HOME=/home/ivan',
        '--setenv=CUDA_VISIBLE_DEVICES=' + GPU, '--setenv=H3_A_LOWMEM_ENABLE=1',
        '--setenv=H3_A_LOWMEM_CHUNK_ROWS=512', '--setenv=H3_A_LOWMEM_SCRATCH_MIB=64',
        '/usr/bin/python3', str(ROOT / 'parent-benchmark-code/a_lowmem/launch.py'),
        '--runtime-root', str(RUNTIME), '--port', '18389', '--execute', '--run-as', 'ivan', '--',
        '--reserve-vram', '6', '--disable-pinned-memory', '--disable-auto-launch', '--disable-api-nodes']
    for name in ('input', 'output', 'temp', 'user'):
        arguments += ['--' + name + '-directory', str(worker / name)]
    arguments += ['--database-url', 'sqlite:///' + str(worker / 'user/comfy.sqlite')]
    command(*arguments)
    with httpx.Client(trust_env=False, timeout=3) as client:
        for attempt in range(60):
            try:
                client.get('http://127.0.0.1:18389/system_stats').raise_for_status()
                return {'status': 'worker_started_no_inference', 'unit': UNIT}
            except httpx.HTTPError:
                if command('systemctl', 'show', UNIT, '-p', 'MainPID', '--value') == '0':
                    raise RuntimeError('experimental bootstrap failed; inspect its journal')
                time.sleep(2)
    raise RuntimeError('experimental bootstrap did not become ready')


def hash_file(path):
    checksum = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def prepare_policy(previous_policy=None):
    environment = production()
    sys.path.insert(0, str(SOURCE))
    from app.recipes import RecipeCatalog
    from app.recipe_dispatch import digest, verify_backend, backend_identity
    catalog = RecipeCatalog()
    pid = int(command('systemctl', 'show', UNIT, '-p', 'MainPID', '--value'))
    process = Path('/proc') / str(pid)
    excluded = {'.git', '__pycache__', '.pytest_cache', 'node_modules', '.cache', 'models',
                'output', 'user', 'input', 'temp', 'tmp', 'venv', '.venv', 'env'}
    files = sorted(path for path in RUNTIME.rglob('*.py') if not excluded.intersection(path.relative_to(RUNTIME).parts))
    files += [RUNTIME / 'extra_model_paths.yaml']
    files += sorted((ROOT / 'parent-benchmark-code/a_lowmem').glob('*.py'))
    manifest = [{'path': str(path), 'sha256': hash_file(path)} for path in files]
    mapping = json.loads((RUNTIME / 'extra_model_paths.yaml').read_text())
    categories = {'base': 'diffusion_models', 'text_encoder': 'text_encoders',
                  'video_vae': 'vae', 'audio_vae': 'vae', 'lora': 'loras'}
    weights = []
    previous_weights = {} if previous_policy is None else {item['filename']: item for item in
        json.loads(previous_policy.read_text())['backends'][0]['weight_files']}
    for weight in catalog.get('A4_C0')['weights']:
        category = categories[weight['role']]
        candidates = [Path(group['base_path']) / group[category] / weight['filename'] for group in mapping.values()]
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise RuntimeError('missing weight ' + weight['filename'])
        previous = previous_weights.get(weight['filename'])
        checksum = previous['sha256'] if previous and previous['path'] == str(path) else hash_file(path)
        if weight.get('sha256') and checksum != weight['sha256']:
            raise RuntimeError('recipe weight mismatch')
        weights.append({'filename': weight['filename'], 'path': str(path), 'sha256': checksum})
    backend = {'id': 'main-lowmem-512', 'lane_id': 'main', 'gpu_uuid': GPU, 'url': 'http://127.0.0.1:18389',
        'pid': pid, 'start_ticks': process.joinpath('stat').read_text().rsplit(')', 1)[1].split()[19],
        'cmdline_sha256': hashlib.sha256(process.joinpath('cmdline').read_bytes()).hexdigest(),
        'cgroup_path': '/sys/fs/cgroup' + process.joinpath('cgroup').read_text().strip().split('::')[1],
        'runtime_root': str(RUNTIME), 'runtime_files': manifest, 'runtime_version': digest(manifest),
        'weight_files': weights, 'recipes': {recipe: {'recipe_version': catalog.get(recipe)['version'],
            'qualification': 'trial', 'vram_budget_bytes': 11000 * 1024 ** 2} for recipe in ('A4', 'A4_C0')}}
    verify_backend(backend, catalog)
    backend_identity(backend)
    policy = json.loads((SOURCE / 'config/recipe-scheduling.json').read_text())
    policy.update(enabled=True, experimental_only=True, resource_profile='residual_zram',
                  production_fence_url='http://100.96.79.21:8789', backends=[backend], validation_cases=['A4', 'A4_C0'])
    private_file(ROOT / 'smoke-policy-r3.json', json.dumps(policy, indent=2))
    private_file(ROOT / 'smoke-key-r3', environment[b'H3_ROUTER_KEY'].decode())
    values = {'H3_ROUTER_KEY': environment[b'H3_ROUTER_KEY'].decode(),
        'H3_FLEET_LANES': environment[b'H3_FLEET_LANES'].decode(),
        'H3_RECIPE_POLICY': str(ROOT / 'smoke-policy-r3.json'), 'H3_FLEET_DATABASE': str(ROOT / 'smoke-r3.sqlite3'),
        'H3_LANE_DATA_ROOT': str(ROOT / 'smoke-lanes')}
    private_file(ROOT / 'smoke-r3.env', '\n'.join(name + '=' + json.dumps(value) for name, value in values.items()) + '\n')
    return {'status': 'experimental_policy_pinned', 'runtime_version': backend['runtime_version'], 'pid': pid,
            'weights_require_full_verification_on_fleet_start': True}


def main():
    parser = argparse.ArgumentParser(description='Isolated Ivan smoke setup; never submits inference or changes production')
    parser.add_argument('phase', choices=['worker', 'policy'])
    parser.add_argument('--previous-policy', type=Path)
    args = parser.parse_args()
    print(json.dumps(start_worker() if args.phase == 'worker' else prepare_policy(args.previous_policy), indent=2))


if __name__ == '__main__':
    main()
