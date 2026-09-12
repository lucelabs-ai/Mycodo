# The `luce` branch

This fork tracks upstream [kizniche/Mycodo](https://github.com/kizniche/Mycodo). Branch `luce` = the upstream release
tag plus the fixes and modules Luce Labs needs on its controllers, **one commit per change**, so that:

- every controller installed by `leaf_pi/installer` runs exactly this code (`git clone -b luce`), not upstream plus hand patches;
- each fix can be opened upstream as a pull request when it is general (the camera and iframe fixes are);
- the team's own modules live where Mycodo expects them: `mycodo/inputs/luce_*.py`, `mycodo/outputs/luce_*.py`,
  `mycodo/devices/luce_*.py`, each with a test under `mycodo/tests/`.

Rules: never commit to `master` here (it mirrors upstream); rebase `luce` onto the next upstream release when we move;
tag each installable state `luce-vN`. The patch scripts that used to be run by hand are documented in
`leaf_pi/metadata_sync/README.md` and are now the first commits of this branch.
