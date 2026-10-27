"""Launch an isolated local permission panel without changing existing services."""
import argparse
import os
from pathlib import Path
from .permissions import PermissionStore
from .server import OwnerServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True, help='Only this explicitly selected read-only directory is registered')
    parser.add_argument('--resource-id', default='workspace')
    parser.add_argument('--label', default='Selected workspace')
    parser.add_argument('--port', type=int, default=8793)
    parser.add_argument('--container-listen', action='store_true', help='Container bridge listener; use only the supplied loopback-published Compose service')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Choose a port from 1024 to 65535')
    if args.container_listen and os.environ.get('MAINAIGER_CONTAINER') != '1':
        parser.error('Container listener requires MAINAIGER_CONTAINER=1')
    os.umask(0o077)
    store = PermissionStore(args.state_dir)
    store.register(args.resource_id, args.label, args.workspace)
    server = OwnerServer(('0.0.0.0' if args.container_listen else '127.0.0.1', args.port), store, container_mode=args.container_listen)
    print(f'Local owner panel: http://127.0.0.1:{args.port}', flush=True)
    print(f'Owner key file: {store.owner_path} (keep private; never enter this key on mainaiger.org)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
