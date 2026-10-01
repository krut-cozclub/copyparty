# copyparty built straight from this source tree, plus the tools
# which the web-commands feature (--wcmd) is most useful with
#
# build from the root of the repo:
#   docker build -t copyparty-fork -f scripts/docker/fork/Dockerfile .
#
# the web-ui needs some third-party files (fonts, markdown editor, ...)
# which are not in git; they are taken from the official pypi release

FROM    alpine:3.22 AS webdeps
ARG     CPP_VER=""
RUN     apk --no-cache add py3-pip unzip
COPY    copyparty/__version__.py /v.py
RUN     ver="${CPP_VER:-$(awk -F'[(,) ]+' '/^VERSION *=/{print $2"."$3"."$4;exit}' /v.py)}" \
        && echo "taking webdeps from copyparty $ver" \
        && { pip download -q --no-deps --dest /dl "copyparty==$ver" \
             || { echo "WARNING: $ver is not on pypi; using latest"; pip download -q --no-deps --dest /dl copyparty; }; } \
        && unzip -q /dl/copyparty-*.whl 'copyparty/web/deps/*' -d /x \
        && ls /x/copyparty/web/deps

FROM    alpine:3.22
LABEL   org.opencontainers.image.title="copyparty-fork" \
        org.opencontainers.image.description="copyparty with server-side unzip and web-commands (tmux / aria2 / 7z / yt-dlp)" \
        org.opencontainers.image.licenses="MIT"
ENV     XDG_CONFIG_HOME=/cfg \
        PYTHONPATH=/opt/cpp \
        PYTHONDONTWRITEBYTECODE=1

# ffmpeg is used by yt-dlp to merge video+audio, and by copyparty for video thumbnails
RUN     apk --no-cache add \
            python3 py3-jinja2 py3-pillow \
            tmux aria2 7zip unzip tini ffmpeg ca-certificates quickjs

# the alpine yt-dlp package lags behind, and yt-dlp must be recent to keep
# working with most sites; this is the official release, which can also
# update itself in a running container with "yt-dlp -U".
# youtube needs a javascript runtime; quickjs is tiny compared to deno
ADD     https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp /usr/local/bin/yt-dlp
RUN     chmod 755 /usr/local/bin/yt-dlp \
        && echo "--js-runtimes quickjs" > /etc/yt-dlp.conf \
        && yt-dlp --version

COPY    copyparty /opt/cpp/copyparty
# web/a/* are symlinks in git; replace them with the real files
RUN     cd /opt/cpp/copyparty/web/a && rm -f u2c.py partyfuse.py webdav-cfg.txt
COPY    bin/u2c.py bin/partyfuse.py /opt/cpp/copyparty/web/a/
COPY    contrib/webdav-cfg.bat /opt/cpp/copyparty/web/a/webdav-cfg.txt
COPY    --from=webdeps /x/copyparty/web/deps/ /opt/cpp/copyparty/web/deps/
COPY    scripts/docker/fork/initcfg scripts/docker/fork/cpp.sh /z/

RUN     mkdir -p /state /cfg /w \
        && chmod 777 /state /cfg /w \
        && python3 -m compileall -q /opt/cpp/copyparty \
        && python3 -m copyparty --version

WORKDIR /state
EXPOSE  3923
ENTRYPOINT ["/sbin/tini", "--", "/bin/ash", "/z/cpp.sh", "-c", "/z/initcfg"]
