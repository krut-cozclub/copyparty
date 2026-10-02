# setup guide: copyparty-fork in practice

notes and lessons from setting up this fork on a home server (debian + docker compose + a reverse proxy + cloudflare); everything here was tested, and the cloudflare parts were checked against cloudflare's documentation

placeholders used below -- replace them with your own:

| placeholder | meaning |
|--|--|
| `cp.example.com` | the domain copyparty is reachable at |
| `you@gmail.com` | your google/github login email |
| `CHANGEME` | a long random password |
| `x-cpp-SECRET` | a random secret header name, see [cloudflare access](#cloudflare-access-login-with-googlegithub) |

* [what the fork adds](#what-the-fork-adds)
* [switching an existing docker-compose setup](#switching-an-existing-docker-compose-setup)
* [example config](#example-config)
* [unzip a file](#unzip-a-file)
* [server commands (the `>_` tab)](#server-commands-the-_-tab)
* [without docker (single python file)](#without-docker-single-python-file)
* [cloudflare access (login with google/github)](#cloudflare-access-login-with-googlegithub)
* [adding more people](#adding-more-people)
* [webdav / mapping a drive in windows](#webdav--mapping-a-drive-in-windows)
* [reading the logs](#reading-the-logs)
* [updating when upstream copyparty releases a new version](#updating-when-upstream-copyparty-releases-a-new-version)
* [known limitations](#known-limitations)


## what the fork adds

* **unzip** -- extract zip / cbz / tar / tar.gz / tar.bz2 / tar.xz on the server from the web-ui; see [README.md](README.md#unzip) for the permission and safety details
* **server commands** -- admin-defined commands (aria2c, yt-dlp, 7z, ...) started from a `>_` tab in the web-ui, running in tmux; see [README.md](README.md#web-commands)
* **docker image** `ghcr.io/<owner>/copyparty-fork:latest` (amd64 + arm64) -- everything from the official `copyparty/ac` image, plus impacket (smb), tmux, aria2, 7zip, unzip, yt-dlp (with quickjs for youtube) and ffmpeg
* **single-file** `copyparty-fork-sfx.py` attached to every github release

none of this is in upstream copyparty, and must not be submitted there: upstream's [CONTRIBUTING.md](../../CONTRIBUTING.md) does not accept AI-written code; see [upstream-feature-request.md](upstream-feature-request.md) for a draft feature-request instead


## switching an existing docker-compose setup

if you were using `copyparty/ac` (or a local Dockerfile like `FROM copyparty/ac` + `pip install impacket`), only the image changes:

```yaml
services:
  copyparty:
    image: ghcr.io/<owner>/copyparty-fork:latest
    # build: .   <- remove; the local Dockerfile is no longer needed
```

everything else keeps working as before: `user: "1000:1000"`, `LD_PRELOAD` for mimalloc, volumes, ports, the healthcheck (wget is included), ftp / sftp / smb / zeroconf

```bash
docker compose pull && docker compose up -d
```

tested with `user: 1000:1000` + mimalloc enabled: http, ftp, sftp, mimalloc, healthcheck, unzip, and yt-dlp jobs all work as uid 1000

notes:
* `yt-dlp -U` cannot update itself when the container runs as a non-root user; pull a newer image instead
* tmux runs inside the container, so restarting the container stops running jobs (they show up as "killed")
* the volume folders (`/w` etc.) must be real mounts; otherwise copyparty refuses uploads since they would be lost with the container


## example config

a complete `copyparty.conf` combining everything in this guide:

```yaml
[global]
  e2dsa                # index files (search, upload-undo, ...)
  e2ts                 # media tags
  ansi

  # reverse-proxy: the docker network the proxy is on
  xff-src: 172.25.0.0/16
  rproxy: -1
  acao: https://cp.example.com

  ftp: 3921
  ftp-pr: 12000-12099
  sftp: 3922
  sftp-pw
  smbw
  smb-port: 3945
  z                    # zeroconf; announce on the lan
  vc-exit

  dav-auth             # windows webdav needs this when volumes allow anonymous read

  # login through cloudflare access (see below)
  idp-hm-usr: ^Cf-Access-Authenticated-User-Email^you@gmail.com^krut
  idp-h-key: x-cpp-SECRET
  idp-logout: /cdn-cgi/access/logout

  # server commands for the >_ tab
  wcmd: dl=aria2c --dir={dir} -- {arg}
  wcmd: ytdl=yt-dlp --no-progress -P {dir} -- {arg}
  wcmd: ytmp3=yt-dlp --no-progress -x --audio-format mp3 -P {dir} -- {arg}
  wcmd: 7z=7z x -y -o{dst} -- {src}

[accounts]
  krut: CHANGEME       # used by ftp / sftp / smb / lan / webdav

[/]
  /w
  accs:
    r: *
    rwmda: krut

[/2]
  /a
  accs:
    r: *
    rwmda: krut
```

* use a real password; ftp / sftp / smb / lan access do not go through cloudflare, so this password is what protects them
* `flags: e2ds` on each volume is unnecessary when `e2dsa` is in `[global]`
* `r: *` lets anyone read without logging in (on the lan, over ftp/smb, and through cloudflare for anyone allowed in); remove it if that's not what you want
* changes to `wcmd` / `[global]` need a restart (`docker compose up -d`), not just a config reload


## unzip a file

1. log in (you need write-access in the destination)
2. select the archive: click its row in the **size or date column** (clicking the filename downloads it instead)
3. click **🗜 unzip** in the bottom-right toolbar, or right-click the file and choose **extract archive here...**
4. pick the destination folder; default is a new folder named after the archive. Tick **overwrite existing files** to replace files (needs delete-access); otherwise they are skipped
5. OK; a message shows how many files were extracted, and lists anything skipped and why

* the button stays greyed-out unless exactly one archive is selected
* for `.rar` / `.7z`, use the `7z` server command instead
* if the button is missing after updating: `docker compose pull && docker compose up -d`, then ctrl-shift-r in the browser
* from a terminal: `curl -X POST -u krut:CHANGEME 'https://cp.example.com/some.zip?unzip=/some/'`


## server commands (the `>_` tab)

* only visible to users with the admin-permission (`a`) in the current folder
* pick a command, fill in the argument (url) / source file / destination folder, click run; the jobs list shows status, the log, and a kill button
* commands are never run through a shell, and `{arg}` cannot start with a dash, so a pasted url cannot inject commands or options
* jobs run in tmux; attach with `docker exec -it copyparty tmux attach -t cpp-JOBID`
* defined with `wcmd:` lines in the config (one per command) or `--wcmd` arguments; both are combined; see `--help-wcmd`
* youtube needs a javascript-runtime; the docker image configures quickjs for yt-dlp, so the "No supported JavaScript runtime" warning should not appear


## without docker (single python file)

every release has `copyparty-fork-sfx.py`; it only needs python 3, and works on linux, macos and windows:

```bash
wget https://github.com/<owner>/copyparty/releases/latest/download/copyparty-fork-sfx.py
python3 copyparty-fork-sfx.py -c copyparty.conf
```

* change the volume paths in the config to the real folders (`/w` and `/a` are docker-only paths)
* unzip, ftp and tftp work out of the box; install whatever else you need:
  * the programs your `wcmd:` lines use, plus tmux: `sudo apt install tmux aria2 7zip ffmpeg`, and yt-dlp
  * thumbnails: `python3-pil` / `ffmpeg`; sftp: `python3-paramiko`; smb: `pip install --user impacket==0.13.0`
* the sfx built by github actions reports its version as plain `1.20.24`; it is still the fork (`--help` lists `--wcmd` and `--no-unzip`)


## cloudflare access (login with google/github)

copyparty has no built-in google/github login, but it supports identity providers: a proxy in front of copyparty does the login and passes the user's identity in a trusted http-header. Cloudflare Access does that for free (free plan, up to 50 users; it asks for a payment method but does not charge)

### 1. zero trust and login methods

1. https://one.dash.cloudflare.com -- pick a team name; the login page becomes `<team>.cloudflareaccess.com`
2. **Zero Trust → Integrations → Identity providers → Add new**
   * **google**: in https://console.cloud.google.com create a project, configure the **OAuth consent screen** (audience: external), then **Create OAuth client** (web application) with
     * authorized javascript origins: `https://<team>.cloudflareaccess.com`
     * authorized redirect uri: `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`
   * **github**: github → settings → developer settings → oauth apps → new; homepage `https://<team>.cloudflareaccess.com`, callback `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`
   * paste client id + secret into cloudflare, then click **Test**

### 2. protect the domain

**Zero Trust → Access controls → Applications → Create new application → Self-hosted and private → Add public hostname** `cp.example.com` (one application can have several hostnames); select the login methods; add a policy: **Allow**, include → **Emails** → `you@gmail.com`

after logging in, cloudflare adds `Cf-Access-Authenticated-User-Email: you@gmail.com` to the requests it forwards to your server

### 3. the secret header (required)

cloudflare's own docs say identity headers alone can be spoofed if the origin can be reached without going through cloudflare; copyparty does not validate cloudflare's signed jwt, so instead it uses `idp-h-key`: the identity headers are only trusted if the request also has a header with a secret **name** (copyparty's code calls this "the primary safeguard for idp")

1. generate a name: `echo "x-cpp-$(openssl rand -hex 12)"`
2. cloudflare dashboard → your domain → **Rules → Overview → Create rule → Request Header Transform Rule**
   * custom filter: hostname equals `cp.example.com`
   * **Set static**: header name = your generated name, value = `1`
   * deploy, and make sure it is enabled
3. put the same name in `idp-h-key:` in copyparty.conf

* transform rules only run on proxied (orange-cloud) dns records; tunnel hostnames count as proxied
* the free plan allows 10 transform rules; names starting with `cf-` / `x-cf-` are not allowed
* never add this header in your local reverse-proxy instead; then anyone reaching the proxy without cloudflare would get it too
* if the name ever leaks (pasted in a chat, a screenshot, ...), generate a new one and update both places

### 4. copyparty

```yaml
[global]
  idp-hm-usr: ^Cf-Access-Authenticated-User-Email^you@gmail.com^krut
  idp-h-key: x-cpp-SECRET
  idp-logout: /cdn-cgi/access/logout
```

`idp-hm-usr` maps an exact email to a copyparty username, so the existing permissions of that user apply; the `[accounts]` password keeps working as a fallback (and is still needed for ftp / sftp / smb / lan)

### 5. verify

1. open `https://cp.example.com`, log in with google/github
2. the top-right of copyparty should say you're logged in as `krut`, and the log should show `@krut` instead of `@*`
3. to see exactly which email cloudflare sends, temporarily add `ihead: cf-access-authenticated-user-email` to `[global]`

### what was tested

with simulated cloudflare headers against the docker image:

| request | result |
|--|--|
| trusted proxy + email + secret header | logged in as `krut` |
| email but no secret header | anonymous (refused) |
| correct headers from an untrusted ip | anonymous (refused) |
| email in different letter-case | anonymous -- emails must match exactly |
| secret header name in lowercase | logged in (header names are case-insensitive) |
| email which is not mapped | anonymous |
| password over the lan | logged in (fallback) |

### client ip and alternatives

* if copyparty's log shows the real visitor ip, leave `xff-hdr` / `rproxy` as they are; if it shows cloudflare ips instead, see the copyparty readme's [real-ip](../../README.md#real-ip) section (`xff-hdr: cf-connecting-ip` + `rproxy: 1`, but only if nothing except cloudflare can send `cf-*` headers to copyparty)
* the most robust setup is a **cloudflare tunnel** (`cloudflare/cloudflared` container, `tunnel --no-autoupdate run` with `TUNNEL_TOKEN`) with **Protect with Access** enabled; then cloudflared validates the access token itself, and no web port needs to be open
* logging out: `/cdn-cgi/access/logout` ends the cloudflare session for all access-protected apps
* cloudflare free plan: max 100 MB per request; the copyparty web uploader splits uploads into chunks so big files still work, but basic-uploader and webdav/rclone uploads over 100 MB will fail through cloudflare


## adding more people

repeat only the `idp-hm-usr` line, one per person; `idp-h-key` and `idp-logout` stay single lines:

```yaml
[global]
  idp-hm-usr: ^Cf-Access-Authenticated-User-Email^you@gmail.com^krut
  idp-hm-usr: ^Cf-Access-Authenticated-User-Email^friend@gmail.com^alice
  idp-hm-usr: ^Cf-Access-Authenticated-User-Email^brother@gmail.com^bob

[/]
  /w
  accs:
    rwmda: krut
    rw: bob      # read + upload
    r: alice     # read only
```

* people who log in through cloudflare do not need an `[accounts]` entry (copyparty logs "unknown, and assumed to come from IdP"); add one with a password only if they also need ftp / sftp / smb
* permissions come from `accs:` -- `r` read, `w` write, `m` move, `d` delete, `a` admin (sees the `>_` tab)
* several emails can map to the same user, but then they share all its permissions
* also add each email to the **Allow** policy of the access application, otherwise cloudflare will not let them in


## webdav / mapping a drive in windows

windows' built-in "map network drive" cannot get through cloudflare access (it cannot do the google login, nor send extra headers)

### from anywhere: rclone + a service token

1. **Zero Trust → Access controls → Service credentials → Service Tokens → Create**; copy the client id and secret (shown once)
2. add a second policy to the access application: action **Service Auth**, include → **Service Token** → that token
3. on windows, install [rclone](https://rclone.org/downloads/) and [winfsp](https://winfsp.dev/rel/)
4. `rclone config file` shows where the config goes; add:

```ini
[cpp]
type = webdav
vendor = owncloud
url = https://cp.example.com/
headers = PW,CHANGEME,CF-Access-Client-Id,XXXX.access,CF-Access-Client-Secret,YYYY
pacer_min_sleep = 0.01ms
```

5. mount it as drive `W:` (put this in a .bat in `shell:startup` to mount on login):

```bat
rclone mount cpp: W: --vfs-cache-mode writes --vfs-cache-max-age 5s --attr-timeout 5s --dir-cache-time 5s --no-console
```

* the service token gets past cloudflare, but has no email, so copyparty logs in with the password (`PW` header)
* uploads over 100 MB fail through cloudflare (rclone uploads each file in one request); use the web-ui for big files
* tested: rclone with these combined headers uploads and lists files fine

### at home: windows' built-in client, directly

1. run `webdav-cfg.bat` as administrator (download it from your server's connect page, `/?hc`); answer Y to allow passwords over http, and it also removes windows' 47.6 MiB download limit
2. this pc → map network drive → `http://192.168.x.x:3923/` (the server's lan ip)
3. type your **password in the username field** (the password field can be anything); this avoids a windows bug where it forgets to resend the password after a reboot

### dav-auth

with `r: *` on a volume, windows decides it does not need to log in, which makes writing impossible; `dav-auth` in `[global]` makes webdav clients always log in (browsers are not affected)


## reading the logs

* `the idp-h-key header (...) is not present in the request; will NOT trust the other headers` -- cloudflare sent the email but not the secret header; the transform rule is missing, disabled, or has a different header name. This is the safety check working as intended
* `got IdP headers from untrusted source` -- the request came from an ip outside `xff-src`
* `invalid password: '...'` -- a login attempt with a wrong password (often an old browser-autofill)
* `job ... finished; rc=None` -- a server command was stopped (killed from the ui, or the container restarted)
* `uploads temporarily blocked due to indexing` -- normal after a restart while `e2dsa` re-checks files
* smb: logins from impacket's own client and `smbclient` failed in testing, identically on the official `copyparty/ac` + impacket image; so the fork does not change smb behaviour. Copyparty's readme describes its smb support as unsafe and not recommended for wan


## updating when upstream copyparty releases a new version

the fork does not update itself; per upstream release:

```bash
git fetch origin                       # origin = 9001/copyparty
git checkout hovudstraum
git merge origin/hovudstraum           # fix conflicts, if any
python3 -m unittest discover -s tests  # on linux, ideally with tmux installed
git push fork hovudstraum
git tag -a v1.21.0-fork.1 -m "copyparty v1.21.0 + fork features"
git push fork v1.21.0-fork.1
gh release create v1.21.0-fork.1 --repo <owner>/copyparty --generate-notes
```

publishing the release triggers `.github/workflows/fork-docker.yml`, which builds the docker image (amd64 + arm64, pushed to ghcr as `:latest`) and the single-file sfx, and attaches both to the release; then on the server: `docker compose pull && docker compose up -d`

* conflicts are most likely in `httpcli.py` and `browser.js`; the fork's additions there are self-contained blocks (search for `unzip` / `wcmd`)
* fork tags must look like `vX.Y.Z-fork.N`; the sfx build hides them so its version check only sees upstream tags
* new python modules must also be added to `scripts/sfx.ls`, or the sfx build fails with "unexpected file"


## known limitations

* unzip runs inside one web request: large archives show a busy message with no progress bar, and cannot be aborted
* tar archives have no index, so the size/count limits are enforced while extracting, and hitting one can leave a partial extraction (zip is checked before anything is written)
* the new ui texts are english only
* uploads over 100 MB through cloudflare only work with the web-ui's chunked uploader
