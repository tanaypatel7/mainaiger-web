"""Observe this device's private network (Tailscale) without changing it.

The Devices page shows what Tailscale itself reports: whether this device is logged in
to a tailnet, its tailnet name and addresses, and the peers it can see (name, OS, online,
addresses). The Lab reads this with ``tailscale status --json`` and never runs ``up``,
``login``, ``serve`` or any command that changes the network. Those become reviewed steps
later (roadmap C1); observation first, so the page is truthful on day one and empty rather
than invented when Tailscale is absent.

Everything else about a device (name, OS, chip, memory, free disk) comes from
``models.device()`` and ``platform``; Docker and Ollama state from the modules that own
them. ``peers`` are *observed tailnet members*, not paired MainAIger devices: pairing is
a separate, explicit step that does not exist yet.
"""
import json
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import models as lab_models

MAC_CLI = '/Applications/Tailscale.app/Contents/MacOS/Tailscale'
TIMEOUT = 10
MAX_PEERS = 200


def tailscale_cli(find=shutil.which):
    """Path of the Tailscale CLI, or None. On a Mac the app bundle ships it; elsewhere it is on PATH."""
    if sys.platform == 'darwin' and Path(MAC_CLI).is_file():
        return MAC_CLI
    return find('tailscale')


def _run(args, run=subprocess.run):
    try:
        proc = run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        return None, str(error)
    return proc.returncode, proc.stdout.decode('utf-8', 'replace')


def _peer(entry, is_self=False):
    ips = [ip for ip in entry.get('TailscaleIPs') or [] if isinstance(ip, str)][:4]
    name = (entry.get('HostName') or entry.get('DNSName') or '').strip().rstrip('.') or 'unknown'
    return {'name': name[:80], 'dns_name': str(entry.get('DNSName') or '').rstrip('.')[:120], 'os': str(entry.get('OS') or 'unknown')[:30],
            'online': bool(entry.get('Online')), 'ips': ips, 'self': is_self, 'last_seen': entry.get('LastSeen') if isinstance(entry.get('LastSeen'), str) else None,
            'exit_node': bool(entry.get('ExitNode')), 'tags': [t for t in (entry.get('Tags') or []) if isinstance(t, str)][:10]}


def tailnet(find=shutil.which, run=subprocess.run):
    """Observed Tailscale state. ``available`` False means the CLI is absent; nothing is guessed."""
    cli = tailscale_cli(find)
    if not cli:
        return {'available': False, 'logged_in': False, 'note': 'Tailscale is not installed on this device (no CLI found).', 'self': None, 'peers': [], 'tailnet': None, 'backend': None}
    code, out = _run([cli, 'status', '--json'], run)
    if code is None:
        return {'available': True, 'logged_in': False, 'note': 'The Tailscale CLI could not be run: %s' % out, 'self': None, 'peers': [], 'tailnet': None, 'backend': None}
    try:
        data = json.loads(out)
    except ValueError:
        text = out.strip()[:300]
        return {'available': True, 'logged_in': False, 'note': 'Tailscale answered without JSON (exit %s): %s' % (code, text), 'self': None, 'peers': [], 'tailnet': None, 'backend': None}
    backend = str(data.get('BackendState') or 'Unknown')
    me = data.get('Self') if isinstance(data.get('Self'), dict) else None
    peers_raw = data.get('Peer') if isinstance(data.get('Peer'), dict) else {}
    peers = [_peer(p) for p in peers_raw.values() if isinstance(p, dict)][:MAX_PEERS]
    peers.sort(key=lambda p: (not p['online'], p['name'].lower()))
    current = data.get('CurrentTailnet') if isinstance(data.get('CurrentTailnet'), dict) else {}
    tailnet_name = str(current.get('Name') or data.get('MagicDNSSuffix') or '')[:120] or None
    logged_in = backend == 'Running' and me is not None
    note = {'Running': 'Logged in to the tailnet.', 'NeedsLogin': 'Tailscale is installed but not logged in. Log in from the Tailscale app; the Lab does not do that for you yet.',
            'Stopped': 'Tailscale is installed but stopped.', 'NoState': 'Tailscale has no state yet.'}.get(backend, 'Tailscale backend state: %s' % backend)
    return {'available': True, 'logged_in': logged_in, 'backend': backend, 'note': note, 'tailnet': tailnet_name,
            'self': _peer(me, True) if me else None, 'peers': peers, 'magic_dns': str(data.get('MagicDNSSuffix') or '')[:120] or None}


def this_device(find=shutil.which, run=subprocess.run):
    """This device as the Devices page shows it: identity, hardware, the runtimes it has, the tailnet it is on."""
    try:
        hardware = lab_models.device()
    except Exception:  # never let a hardware probe break the page
        hardware = {'system': platform.system(), 'architecture': platform.machine(), 'chip': platform.processor() or platform.machine(), 'ram_bytes': None, 'free_bytes': None, 'gpu_note': ''}
    docker = bool(find('docker')) or (sys.platform == 'darwin' and Path('/Applications/Docker.app').exists())
    return {'hostname': socket.gethostname(), 'system': hardware['system'], 'architecture': hardware['architecture'], 'chip': hardware['chip'],
            'ram_bytes': hardware['ram_bytes'], 'free_bytes': hardware['free_bytes'], 'python': platform.python_version(),
            'docker_present': docker, 'ollama_running': _safe(lab_models.running), 'role': 'this device (the Lab runs here)'}


def _safe(fn):
    try:
        return bool(fn())
    except Exception:
        return False


def overview(find=shutil.which, run=subprocess.run):
    return {'device': this_device(find, run), 'tailnet': tailnet(find, run),
            'paired': [], 'pairing_note': 'Pairing other MainAIger devices (running Mainbooks there) is not built yet; see docs/INTEGRATION.md step I1. Peers above are what Tailscale sees, nothing more.'}
