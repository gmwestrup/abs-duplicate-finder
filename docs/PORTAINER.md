# Installing with Portainer

Two ways to do it. **Method A** is easiest: Portainer pulls a prebuilt image and you never
touch the source. **Method B** builds the image from GitHub, which is useful if you change
the code yourself.

Before you start, collect three things:

| You need | Where to find it |
|---|---|
| The ABS address | The URL you open Audiobookshelf at, with its port, e.g. `http://192.168.1.50:13378` |
| An ABS API token | ABS → Settings → API Keys. Use an **admin** user if you want to edit anything. |
| The library path ABS uses | ABS → Settings → Libraries → edit your library. It shows the folder, often `/audiobooks`. |

And, on the host, the folder that path points at (the one mounted into the ABS container),
for example `/volume1/Media/Audiobooks`.

---

## Method A: prebuilt image (recommended)

### 1. Publish the image once
Push this repository to GitHub. The included Action builds the image and publishes it to
`ghcr.io/gmwestrup/abs-duplicate-finder`. Then make it pullable without a login:

GitHub → your profile → **Packages** → `abs-duplicate-finder` → **Package settings** →
**Change visibility** → Public.

(If you'd rather keep it private, in Portainer go to **Registries** → **Add registry** →
Custom, URL `ghcr.io`, username `gmwestrup`, password = a GitHub personal access token with
`read:packages`.)

### 2. Create the stack
1. Portainer → **Stacks** → **Add stack**.
2. Name: `abs-dupes`.
3. Build method: **Web editor**.
4. Paste the contents of [`docker-compose.portainer.yml`](../docker-compose.portainer.yml).
5. Edit the lines marked CHANGE ME:
   - the config folder on the left of `:/config`
   - your audiobook folder on the left of `:/library`
   - `ABS_URL`
6. **Deploy the stack**.

### 3. First run
1. Open `http://your-server:5057`.
2. Paste the ABS address and API token, then **Save and test**.
3. For file editing, tick **Allow editing** and set the path mapping to
   `<ABS's library path>=/library`, e.g. `/audiobooks=/library`. The settings page says
   whether the mapped folder was found.
4. Go back, pick your libraries and scan.

### 4. Updating later
Portainer → Stacks → `abs-dupes` → **Editor** → tick **Re-pull image** → **Update the stack**.

---

## Method B: build from the GitHub repository

Use this if you want Portainer to build your own edits.

1. Portainer → **Stacks** → **Add stack** → **Repository**.
2. Repository URL: `https://github.com/gmwestrup/abs-duplicate-finder`
   (add a personal access token under Authentication if the repo is private).
3. Reference: `refs/heads/main`.
4. Compose path: `docker-compose.yml`.
5. Under **Environment variables**, add anything you want to override, or edit the compose
   file in the repo first. Note that this compose file includes `build: .`, so Portainer
   builds the image on your server; that takes a few minutes the first time.
6. Optionally turn on **GitOps updates** so Portainer redeploys when you push changes.
7. **Deploy the stack**.

---

## Notes and troubleshooting

**Paths must be absolute.** In a Portainer stack, `./config` points at Portainer's own
internal stack folder, which is not where you think it is. Always use full host paths like
`/volume1/Docker/abs-dupes/config`.

**The mapping is the part people get wrong.** The left side of `PATH_MAP` is the path *ABS*
shows, not a host path. The right side is the path *inside this container*. So if ABS shows
`/audiobooks` and you mounted your books at `/library` here, it's `/audiobooks=/library`.
If ABS shows the raw host path instead (e.g. `/volume1/Media/Audiobooks`), use that on the
left.

**Talking to ABS over the Docker network.** If Audiobookshelf runs on the same Docker host,
you can use its container name instead of an IP, e.g. `http://audiobookshelf:80`, as long as
both containers share a network. Add to the stack:

```yaml
    networks: [absnet]
networks:
  absnet:
    external: true
    name: <the network your ABS container uses>
```
Otherwise the host IP works fine and needs no extra setup.

**Port already in use.** Change the left side of `"5057:5057"`, e.g. `"5157:5057"`, and open
that port instead.

**File ownership.** The container runs as root, so files it renames keep their ownership but
anything it creates (the trash folder, backups) is owned by root. If that's a problem, add
`user: "1000:100"` to the service and make sure that user can write to both `/config` and the
library folder.

**Nothing is deleted.** Trash is a folder inside the library root with a `.ignore` file so ABS
skips it. Empty it yourself once you're happy.

**Check the logs.** Portainer → Containers → `abs-dupes` → Logs shows every request and any
connection errors.

**`/library` is empty inside the container.** Docker silently creates an empty folder when a
bind mount points at a host path that doesn't exist, and the app then reports "nothing is
there" when you try to rename or trash. Copy the host path from your Audiobookshelf
container's own volume list rather than typing it; on some NAS systems it carries a prefix
such as `/share`. Check with `ls /library` in the container console.

**Permission denied writing to /config.** The folder is owned by root but the container runs
as another user. Either `chown` the folder to that user, or remove the `user:` line so it runs
as root like most NAS containers.
