# Tier 3: grading status

Tier 3 sends uncategorized merchant names to Gemini. It is off unless you configure a key;
setup is in [app/README.md](app/README.md#tier-3) and the design is in
[DESIGN.md](DESIGN.md) Section 3.

## Ungrounded: graded, shipping

`gemini-3.8-flash` was graded on 2026-10-11 against 65 hand-labelled merchants: 48 correct,
5 wrong, 12 abstained (74% correct, 8% wrong). That passes the gate, against a 40%
baseline.

## Grounded: built, shipped off, never graded

The grounded path, which lets the model use Google Search, is behind `GEMINI_GROUNDING`
and off by default. It has never been graded. The API key ran out of credits partway
through the eval, and no grounded run has finished since. It stays off until a grade
exists.

Once you have a working key, this finishes the grade:

```powershell
python spike\eval_categories.py --gemini --grounding
```
