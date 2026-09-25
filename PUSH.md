# Getting this onto GitHub

This folder is already a git repository: two commits on `main`, tagged `v2.0.1`,
authored as gmwestrup. You do not need to run `git init`.

## With GitHub Desktop
1. **File → Add local repository**, choose this folder.
2. **Publish repository**. Name it `abs-duplicate-finder`, untick "Keep this code private"
   if you want it public, and publish.
3. **Repository → Push** (tags go with it).

## With the command line
```bash
cd abs-duplicate-finder
git remote add origin https://github.com/gmwestrup/abs-duplicate-finder.git
git push -u origin main --tags
```

## If the .git folder didn't survive unzipping
Some extractors skip hidden folders. Use the bundle instead:
```bash
git clone abs-duplicate-finder.bundle abs-duplicate-finder
cd abs-duplicate-finder
git remote set-url origin https://github.com/gmwestrup/abs-duplicate-finder.git
git push -u origin main --tags
```

## After the first push
- **Actions** builds the Docker image and publishes it to
  `ghcr.io/gmwestrup/abs-duplicate-finder`. Pushing the `v2.0.1` tag also tags the image.
- Make the image pullable without a login: your GitHub profile → **Packages** →
  `abs-duplicate-finder` → **Package settings** → **Change visibility** → Public.
- Day to day: `git add -A`, `git commit -m "..."`, `git push`. Tag releases with
  `git tag -a v2.1 -m "Version 2.1"` and `git push --tags`.

Your settings, scan results, dismissals and change log are excluded by `.gitignore`,
so your ABS token never leaves your machine.
