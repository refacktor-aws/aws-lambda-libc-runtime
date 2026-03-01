import os
import subprocess
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
dockerfile_path = os.path.join(script_dir, 'Dockerfile')
context_path = os.path.join(script_dir, '..')

build_command = ['docker', 'build', '-t', 'test', '-f', dockerfile_path, context_path]
result = subprocess.run(build_command)

if result.returncode != 0:
    print("Docker build failed", file=sys.stderr)
    sys.exit(1)

run_command = ['docker', 'run', 'test']
result = subprocess.run(run_command)
sys.exit(result.returncode)