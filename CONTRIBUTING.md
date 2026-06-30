# Contributing to Bug Fix Recommender

Thanks for considering a contribution. This is a thesis-grade ML data pipeline —
correctness and reproducibility matter more than speed.

## Before you start

- Open an issue first for anything beyond a trivial fix (typo, docs). Saves you
  rework if the approach doesn't fit the architecture.
- Check `README.md` for system architecture before touching `src/`.
- Pipeline stages (`discovery → downloader → extractor → preprocessing → storage → retrieval`)
  are intentionally decoupled. Don't introduce cross-stage imports.

## Dev setup

```powershell
git clone https://github.com/itsmrsarfaraz/bugfixrecommender.git
cd bugfixrecommender
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pytest tests/ -v   # must pass before you touch anything
```

For the VS Code extension:

```powershell
cd extension
npm install
npm run compile
```

## Branching & commits

- Branch off `main`: `feature/<short-name>`, `fix/<short-name>`, `docs/<short-name>`.
- Use [Conventional Commits](https://www.conventionalcommits.org/):
  `feat(extractor): add Python language adapter`
  `fix(api): handle empty buggy_code in /recommend`
- One logical change per PR. Don't bundle unrelated fixes.

## Tests

- All new logic needs a unit test in `tests/`. No exceptions for `src/extractor`,
  `src/preprocessing`, `src/retrieval` — these are the correctness-critical paths.
- Run `pytest tests/ -v` before opening a PR. CI will reject failing builds.
- Mock GitHub/network calls — tests must run offline (see `tests/test_repo_discovery.py`
  for the pattern).

## Config changes

All thresholds live in `config/config.yaml`, validated by `src/config_loader.py`
(Pydantic). If you add a config key:
1. Add the field + validator in `config_loader.py`.
2. Update `config/config.yaml` with a comment explaining the value.
3. Never hardcode thresholds in `src/`.

## Adding a new language adapter (V2 scope)

1. Subclass `LanguageAdapter` in `src/extractor/language_adapter.py`.
2. Register it in the `ADAPTERS` dict.
3. Add the extension to `extractor.target_extensions` in `config.yaml`.
4. Add adapter-specific tests in `tests/test_commit_extractor.py`.

## Pull requests

- Fill out the PR template.
- Link the issue it closes.
- Keep the diff focused — large refactors need a prior design discussion in an issue.
- A maintainer will review; expect requests for tests or doc updates before merge.

## Code style

- Type hints required on public functions.
- Docstrings explain WHY, not just WHAT (see existing modules for the tone).
- No silent `except: pass` — log and handle explicitly.

## Reporting bugs / requesting features

Use the issue templates under `.github/ISSUE_TEMPLATE/`.
