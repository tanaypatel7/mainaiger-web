# Local Python Lab prototype

This is a first local, single-cell Python editor. It can open and download a
copy of a standard `.ipynb` notebook containing exactly one code cell. Downloaded
notebooks contain the current source with prior outputs cleared. It is separate
from the existing private Nexus installation. It is **not yet a full notebook editor**,
a macOS app installer, a website/device connection, or a remote execution server.

On your own Mac, install and start Docker Desktop and obtain this repository:

```bash
git clone https://github.com/tanaypatel7/mainaiger-web.git
cd mainaiger-web
docker pull python:3.13.18-slim
python3 -m local_lab
```

The first three commands download the code and official Python container image.
The last starts a panel only on your Mac at `http://127.0.0.1:8794`. Its printed
output identifies the path of a private `owner.key` file, not the key itself.
Open that file locally (for the default location: `open -a TextEdit
~/.local/share/mainaiger-lab/owner.key`) and use its contents to unlock the local
panel. Do not enter that key on mainaiger.org or send it to another person.

In the panel, optionally type an absolute path to a folder you want the Python
container to read. An empty field means no host folder is mounted. Choose a CPU
and memory limit, save the settings, then write a small Python cell. Review the
displayed container command and code, and explicitly approve each run. Output is
displayed in the panel. The selected folder is mounted read-only, so the code
cannot save results there in this version. A new container is created for each
run, so variables do not persist across runs.

Runs use a nonroot user, no network, a read-only container root, a temporary
directory, dropped Linux capabilities, CPU/memory/process limits, a 20-second
time limit and capped output. Cleanup after a timeout is best-effort: check
`docker ps --filter name=mainaiger-lab-` for a remaining job if Docker stalls.
The browser does not receive the Docker socket.
The local server invokes a fixed Docker command; it never invokes Python code on
the host. GPU use is not implemented. The sandbox is defense in depth, not a
guarantee against vulnerabilities in Docker or the host OS.

Settings remain in memory while the panel runs and are reset on restart. Stop it
with Ctrl-C. Unmounting or changing a directory requires saving new settings in
the panel. The selected directory may contain sensitive files; Python code can
read everything the container user is permitted to read in that directory.

Docker Desktop and the Python image must be installed separately. The GUI does
not download software or pair with the website. No code, folder path, output or
local key is uploaded to the directory service. Docker image pull uses your
normal network connection; execution containers are configured without network.

This prototype was unit-tested without Docker; the image, actual container
execution, Mac folder permissions and desktop setup have not yet been tested on
a Mac. Next steps include multi-cell notebook editing, persistent kernels, validated
installation packaging, and explicit, authenticated website pairing. Do not
expose this local service to the internet or expand its listener beyond loopback.
