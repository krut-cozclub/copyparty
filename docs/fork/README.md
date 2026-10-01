# fork additions: unzip + web-commands

this fork adds two features to copyparty; neither is in upstream

* [unzip](#unzip) -- extract zip/tar archives on the server, from the web-ui
* [web-commands](#web-commands) -- run admin-defined commands (aria2c, 7z, yt-dlp, ...) from the web-ui, in tmux
* [docker image](#docker-image) -- with tmux, aria2, 7zip, unzip, yt-dlp and ffmpeg preinstalled

> **NOTE:** this code was written with an AI assistant, so it must not be submitted upstream; the copyparty [CONTRIBUTING.md](../../CONTRIBUTING.md) does not accept AI-written code. See [upstream-feature-request.md](upstream-feature-request.md) for a draft feature-request instead.


## unzip

select exactly one archive in the file list and click the 🗜 `unzip` button (or right-click → `extract archive here...`), then choose the destination folder; it defaults to a new folder named after the archive

supported formats: `zip`, `cbz`, `tar`, `tar.gz` / `tgz`, `tar.bz2` / `tbz2`, `tar.xz` / `txz` (detected by contents, not by the file extension)

permissions:
* read-access to the archive
* write-access to the destination (each extracted file is checked separately, so nested volumes are respected)
* overwriting existing files (the checkbox, or `&replace`) also needs delete-access; otherwise existing files are skipped

safety:
* entries with `..`, absolute paths, or drive letters are skipped (no zip-slip)
* symlinks, hardlinks, device files and encrypted entries are skipped
* nothing is ever written into `.hist` (the copyparty database)
* `--unzip-maxn` (default `64k`) and `--unzip-maxs` (default `32g`) limit the number of files and the total extracted size; for zip files this is checked before anything is written, and the actual number of bytes is enforced during extraction (an archive can lie about its sizes)
* volume upload-limits (`vmaxb`, `df`, `sz`, ...) and upload hooks (`xbu` / `xau`) apply to each extracted file, just like uploads
* extracted files are added to the database / search index

disable the feature with `--no-unzip`

api: `curl -X POST -H 'PW: hunter2' 'http://127.0.0.1:3923/music/album.zip?unzip=/music/album/'` returns json with the number of files, bytes, and any skipped entries (and why)


## web-commands

the admin defines named commands at startup; users with the admin-permission (`a`) in a folder can then start them from the `>_` tab in the web-ui, watch the output, and kill them

```bash
copyparty -a admin:hunter2 -v /srv/files::A,admin \
  --wcmd 'dl=aria2c --dir={dir} -- {arg}' \
  --wcmd '7z=7z x -y -o{dst} -- {src}' \
  --wcmd 'ytdl=yt-dlp -P {dir} -- {arg}'
```

or in a config file (`copyparty -c my.conf`, or any `*.conf` in the `/cfg` folder of the docker image); one `wcmd:` line per command, and the other `--wcmd-*` options work the same way (`wcmd-maxj: 2`):

```yaml
[global]
  wcmd: dl=aria2c --dir={dir} -- {arg}
  wcmd: ytdl=yt-dlp --no-progress -P {dir} -- {arg}
  wcmd: 7z=7z x -y -o{dst} -- {src}
```

a complete example is [scripts/docker/fork/example.conf](../../scripts/docker/fork/example.conf); commands from `--wcmd` arguments and config files are combined. Changes need a restart (not just a config reload)

variables which can be used in a command:

| variable | what | permission needed |
|--|--|--|
| `{dir}` | the folder the user is browsing; also the working-directory of the command | write |
| `{src}` | a file the user selected / typed | read |
| `{dst}` | a destination folder the user typed (created if needed) | write |
| `{arg}` | free text, for example a URL | - |

security model:
* commands are **never executed by a shell**; the command is split into arguments when copyparty starts, and each variable is substituted inside one argument, so `x; rm -rf /` or `$(id)` stays one harmless argument
* `{arg}` cannot start with `-` (so users cannot add their own options) and cannot contain control characters; still, put `--` before `{arg}` when the program supports it
* only users with admin-access in the current folder can see / run / kill commands, and they only see jobs which were started in folders where they are admin
* paths are resolved through the copyparty permission system, so `{src}` / `{dst}` cannot point outside the volumes the user has access to
* `--wcmd-maxj` (default 4) limits how many commands can run at the same time
* the feature is off unless at least one `--wcmd` is given

### tmux

if tmux 3.0 or newer is installed, each job runs in a detached tmux session named `cpp-JOBID`; this means jobs keep running when copyparty restarts (they are rediscovered on startup), and you can attach to one on the server to see it live or answer a prompt:

```bash
tmux ls
tmux attach -t cpp-6abe660e677e
```

the output is also saved to a logfile which the web-ui shows. Without tmux (or with `--wcmd-tmux n`), jobs run as normal background processes and are stopped when copyparty stops. Older tmux versions are not used, because they would run the command through a shell

other options: `--wcmd-dir` (where logs are kept), `--wcmd-nkeep` (how many finished jobs to remember), see `--help-wcmd`


## docker image

the image is built from this source tree by [scripts/docker/fork/Dockerfile](../../scripts/docker/fork/Dockerfile), and includes `tmux`, `aria2c`, `7z`, `unzip`, `yt-dlp` (latest release at build time, with the quickjs javascript-runtime for youtube), `ffmpeg` (yt-dlp needs it to merge video+audio; also gives copyparty video thumbnails), and pillow (image thumbnails)

yt-dlp breaks when sites change; update it inside a running container with `docker exec CONTAINER yt-dlp -U` (lost when the container is recreated), or add a command for it: `wcmd: ytdl-update=yt-dlp -U`

pull it (amd64 and arm64; built by [.github/workflows/fork-docker.yml](../../.github/workflows/fork-docker.yml) for each release):

```bash
docker pull ghcr.io/krut-cozclub/copyparty-fork:latest
```

or load a tarball from the [github release](https://github.com/krut-cozclub/copyparty/releases):

```bash
# pick amd64 for normal pcs, arm64 for raspberry pi 4/5 or apple silicon
gunzip -c copyparty-fork-amd64.tar.gz | docker load
```

(the examples below say `copyparty-fork`; use `ghcr.io/krut-cozclub/copyparty-fork` if you pulled it)

or build it yourself:

```bash
docker build -t copyparty-fork -f scripts/docker/fork/Dockerfile .
```

run it; the folder you want to share goes into `/w`, and config/state goes into `/cfg`. The easiest is to copy [example.conf](../../scripts/docker/fork/example.conf) into your config folder, change the password, and run:

```bash
docker run --rm -it -p 3923:3923 \
  -v /path/to/your/files:/w \
  -v /path/to/config:/cfg \
  copyparty-fork
```

or without a config file, everything as arguments:

```bash
docker run --rm -it -p 3923:3923 \
  -v /path/to/your/files:/w \
  -v /path/to/config:/cfg \
  copyparty-fork \
  -a admin:CHANGEME -v /w::A,admin -e2dsa \
  --wcmd 'dl=aria2c --dir={dir} -- {arg}' \
  --wcmd '7z=7z x -y -o{dst} -- {src}'
```

then open http://127.0.0.1:3923/, log in with the password, and

* upload a zip, select it, click 🗜 `unzip`
* click the `>_` tab, pick `dl` or `ytdl`, paste a URL, click run

attach to a running job with `docker exec -it CONTAINER tmux attach -t cpp-JOBID`

the `/w` folder must be a real mount (`-v`); otherwise copyparty refuses uploads since they would be lost when the container is removed


## tests

```bash
python3 -m unittest tests.test_unzip tests.test_wcmd
```

the tmux test only runs if tmux 3.0+ is installed
