# DRAFT -- not posted anywhere

this is a draft feature-request which could be posted to upstream copyparty (github issue or discord), to be reviewed and ideally rewritten in your own words first

important: copyparty's CONTRIBUTING.md says "do not use AI / LLM when writing code"; the implementation in this fork was AI-assisted, so it should **not** be offered as a PR or patch. The request below only describes the idea; if accepted, a human would write the code (or the maintainer would)

---

## feature request: extract archives on the server from the web-ui

**the problem**

when someone uploads a zip/tar (for example a folder of photos zipped on a phone, or a release tarball), the only way to get the files out is to download it, extract it locally, and upload the files again. For big archives on a slow connection that's painful, and impossible on some phones

**the idea**

a button in the file manager (next to rename / delete / cut / copy / paste), enabled when exactly one archive is selected, which asks for a destination folder (default: a new folder named after the archive) and extracts the archive there, server-side

* formats: zip and tar (gz/bz2/xz); all in the python stdlib, so no new dependencies
* permissions: read-access to the archive, write-access to the destination, delete-access to overwrite existing files (otherwise they are skipped)
* new files would go through the same machinery as uploads: volume limits (vmaxb / df / sz), xbu/xau hooks, and indexing

**security considerations** (probably the main reason this needs careful thought)

* zip-slip: entries with `..`, absolute paths, or drive letters must be skipped
* symlinks / hardlinks / device files inside archives should be skipped
* never write into `.hist`
* zip-bombs: a limit on number of files and total extracted bytes, enforced on actual bytes written, not on the sizes the archive claims
* should it be off by default, or limited to some permission (like `--zip-who`)?

**related idea: admin-defined commands**

more generally, it could be useful to let the server admin define a few commands (`aria2c` to download a URL into the current folder, `7z x` for rar/7z, `yt-dlp`, ...) which admins could start from the web-ui, running in a detached tmux session so they survive restarts and can be attached to. This is a much bigger security surface (no shell, strict argument substitution, admin-only), so it may be better suited as a hook/plugin than as a core feature; just mentioning it in case it fits with the existing hooks

**questions for the maintainer**

* would server-side extraction fit copyparty, or is it better as a hook (e.g. an `xau` hook which extracts uploaded archives)?
* preferred UI placement and permission model?
