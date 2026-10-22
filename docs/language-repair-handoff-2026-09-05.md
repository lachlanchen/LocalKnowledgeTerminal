> Superseded by `language-repair-handoff-2026-09-06.md`: deployed and validated on the Pi.

# General language repair: local implementation handoff

## User requirement

Repair missing language content and detect apparent imperfections across all
words, not just stout. Use local Qwen with retrieved books/dictionaries.
Keep useful partial results visible, preserve provenance, and avoid retry loops.

## Live diagnosis

Stout source entity: term-70ec9180-bb09-5dc3-a5a0-fd6f2095deb8.

The Chinese translation job
job-652a86aa-51eb-5df8-8e64-d691c0c6efaa exhausted two attempts with
"Chinese translation has no Han characters". Both recorded model drafts
returned the English source word as the translated term, although the meaning
field contained Chinese prose. Its pronunciation dependency was then marked
failed. The partial Word Card was published and no durable language-specific
recovery resumed that failed work.

This is an initial model mistake plus a recovery-logic gap, not evidence of
SQLite file corruption. The latest live card has no accepted Chinese
translation row or Chinese language atom.

Other general problems found:

- Translation prompts required choosing an exact dictionary candidate even
  when a headword dictionary supplied another sense. Arabic normalization
  could overwrite a valid model translation with a sole dictionary candidate.
- Live language projection was extracted from a size-limited diagram graph,
  so accepted translations could disappear when their nodes were omitted.
- An older translation reading could overwrite a dictionary-backed reading
  during live projection. An old pronunciation could also be applied to a
  changed translation form.

## Local changes, not deployed

- lkt/language_repair.py: persistent language observations, bounded cursor
  scanning, missing-data and apparent-imperfection detection, targeted
  retrieval/translation/pronunciation/composition job chains, durable cooldown,
  one repair chain at a time, and six other completions between chains.
- lkt/language_projection.py: shared language-card JSON assembled from
  accepted language checkpoints rather than bounded diagram traversal.
  Current pronunciation must match the current translation form and target.
- lkt/atomic.py: shared projection adapter, sense-aware candidate guidance,
  error/observation feedback and retrieved context on repair prompts, fresh
  local-model review instead of deterministic copying during repairs, and
  no forced replacement of a valid Arabic answer with a different candidate.
- lkt/web.py: use the independent language projection.
- lkt/cli.py: admit the language repair seeder through the existing bounded
  background-planning callback.
- tests/test_language_repair.py: nine regression tests for general detection,
  isolation, cooldown, structural gaps, projection, readings, and sense choice.

Suspected issues are observations, not automatic deletion decisions.
The detector includes missing translations/targets/sense links, unexpected
script, replacement characters or apparent TeX contamination, uninformative
definitions, unsupported unchanged forms, dictionary sense review, changed
source senses, missing current pronunciation, and reading discrepancies.
It cannot guarantee that every semantic mistake will be detected or corrected.

## Known implementation error awaiting requested approval

In LanguageRepairSeeder.observe, the insert into lexical_language_health
has **11 placeholders for 10 columns and 10 supplied values**. Change the
placeholder list to ten entries, preferably naming the columns explicitly.
The assistant asked for approval to correct this error and continue testing.
No approval response had arrived when this handoff was written.

The live Pi was not modified in this task.

## Validation so far

- Seven new regression tests passed.
- Two new queue/cooldown tests errored on the known placeholder-count issue.
- All 54 existing tests in tests.test_multilingual_pipeline,
  tests.test_cli, and tests.test_web passed.
- No full-suite pass, deployment, or live automatic repair is claimed.

Logs: /tmp/lkt-language-repair-tests.log and
/tmp/lkt-language-existing-tests.log.

## Next continuation

1. Correct the flagged insert after the requested approval, rerun the new
   tests, and run the appropriate broader regression checks.
2. Exercise a complete repair chain against a disposable database and actual
   book evidence, including preservation of unrelated languages.
3. Ensure reading mismatch repairs converge in storage, not only presentation:
   pronunciation can correct the displayed reading while an older translation
   checkpoint still retains its original reading.
4. Verify recovery after worker restart and after an unsuccessful model review.
5. Back up current live databases and source before deployment. Check that
   live source has not changed unexpectedly; preserve unrelated changes.
6. Deploy with a graceful worker replacement, not by killing an active model
   request. Do not restart Qwen or alter networking.
7. Scan existing prepared words, repair through the same general queue, and
   verify stout plus other words through live card JSON and browser output.

Use the preceding growth/heartbeat handoff for Pi paths and runtime revisions.
