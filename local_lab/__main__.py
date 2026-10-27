"""Run the local Lab panel. This does not start a public listener."""
import argparse
import os
from pathlib import Path
from .server import LabServer, initialize_state

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/share/mainaiger-lab')
    parser.add_argument('--port', type=int, default=8794)
    args=parser.parse_args()
    if not 1024<=args.port<=65535: parser.error('Port must be between 1024 and 65535')
    os.umask(0o077)
    state,key=initialize_state(args.state_dir)
    server=LabServer(('127.0.0.1',args.port),state,key)
    print(f'Lab panel: http://127.0.0.1:{args.port}',flush=True)
    print(f'Local owner key file: {state / "owner.key"} (never enter it on mainaiger.org)',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=='__main__': main()
