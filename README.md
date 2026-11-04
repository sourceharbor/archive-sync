# SourceHarbor upstream preservation

Independent public source preservation. Not affiliated with or maintained by Storytold.

The [repository list](repositories.json) identifies 39 ArtCraft-related projects and their
supporting libraries, assets, test corpora, and development tools. Source code stays in
its own public fork under [SourceHarbor](https://github.com/sourceharbor).

## Automatic updates

[Preserve upstream source](../../actions/workflows/sync.yml) runs every six hours
at 00:23, 06:23, 12:23, and 18:23 UTC, or through **Run workflow**. GitHub can delay
scheduled jobs. Upstream push events do not reach this organization; immediate
push-triggered synchronization would require upstream webhook/dispatch cooperation.

Each check copies new branches and tags and fast-forwards changed branches. It never
force-pushes or deletes destination references. Changed existing tags and diverged
branches fail that repository's atomic update and leave it intact. Missing upstreams
also leave the saved copy untouched. Other repositories continue independently;
any failure fails the overall run. Up to four repositories sync concurrently, with a
ten-minute limit per repository and at most 40 configured repositories. Partial
progress is recorded after each repository. Inspect [Actions](../../actions) for failures and
[status.json](status.json) for the latest completed per-repository results.

Recording each check updates this controller repository and provides useful activity
so its schedule does not go dormant after GitHub's 60-day public-repository cutoff.
A disabled schedule or prolonged outage still requires operator attention.

## Security and cost

Actions are disabled repository-wide on every preserved fork. Upstream code is never
checked out or executed by this controller. Only this controller's reviewed script
runs. One SSH deploy key per fork allows writes to that fork alone; private keys are
stored in the controller's `SYNC_DEPLOY_KEYS` Actions secret. Recovery copies are kept
in the owner's protected credential vault, not this repository. GitHub SSH host keys
are pinned in `github_known_hosts`; verify official GitHub announcements before
changing them. The controller's built-in token only writes its own status record.

Standard GitHub-hosted Linux runner compute is free for this public repository.
There are no artifact uploads, caches, paid runners, or builds. Deploy keys do not
expire automatically: revoke a key in the destination repository if it is compromised
or that repository is removed from this service.

## Maintenance

To add another public project, create a public fork, disable its Actions, provision a
unique write-enabled deploy key, add its private key to `SYNC_DEPLOY_KEYS` keyed by
destination repository name, and add the upstream/name/reason entry to
`repositories.json`. Review and test the change before merging. The current script
restricts source owners to `storytold`; review that allowlist when adding a new source.
Do not enable inherited workflows on preserved forks or use these forks as working
branches; use a separate development repository for modifications.

Run `python3 -m unittest -v test_sync.py` to test the preservation rules locally.

## Preservation limits

This preserves Git branches, tags, and reachable history, not a complete GitHub export.
Git LFS pointers are preserved but LFS object bytes are not independently backed up.
Release binaries, issues, discussions, external assets, package registries, and hosted
services are not copied. No build or runnability guarantee is implied. Original
licenses and attribution remain in each fork. A separate offline backup is still
needed to protect against loss of GitHub itself.

`storytold/AniCraft` was empty at setup and GitHub refused to fork it. It is not enrolled.
