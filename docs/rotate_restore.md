# Capital Rotation — `/rotate` and `/restore`

Temporarily redeploy capital from a held position into a higher-momentum trade, then restore the original holding once enough profit has been captured — without losing track of what needs to come back.

---

## The Idea

You hold 100 shares of AAPL.  
You spot a short-term setup in PLTR.  
You sell 50 AAPL to fund a PLTR trade, then after PLTR gains 10% you sell PLTR and buy back the 50 AAPL — ideally with proceeds left over.

**Net result:** same AAPL quantity as before, plus profit extracted from the PLTR trade.

---

## Commands

### `/rotate FROM SIZE TO [+X%]`

Sell `SIZE` of `FROM`, immediately buy `TO` with the proceeds.  
Optionally place a take-profit limit on `TO` at `+X%` above the current ask.  
The rotation is saved to `data/rotations.json` so you don't have to remember the qty.

| Argument | Meaning |
|----------|---------|
| `FROM`   | Symbol you're pulling capital from (must have an open position) |
| `SIZE`   | Shares, `all`, `N%`, `eN` euros, or `$N` USD |
| `TO`     | Symbol you're deploying into |
| `+X%`    | Optional. Places a limit-sell on `TO` at `X%` above the current ask |

**Examples:**
```
/rotate AAPL 50 PLTR           sell 50 AAPL, buy PLTR at market
/rotate AAPL e3000 PLTR +10   sell €3000 of AAPL, buy PLTR, set TP at +10%
/rotate AAPL all PLTR +8.5    rotate everything, set TP at +8.5%
```

After the buy fills, the CLI prints:
```
────────────────────────────────────────────────────
  Rotation open:  50 AAPL → 312 PLTR
  To restore:     /restore PLTR AAPL 50
────────────────────────────────────────────────────
```

---

### `/restore FROM TO [QTY]`

Sell all of `FROM` (the temp position) at market, then buy back `QTY` shares of `TO` (the original holding).

If the rotation was opened with `/rotate`, `QTY` is **optional** — it is read from the saved record automatically.

| Argument | Meaning |
|----------|---------|
| `FROM`   | The temp symbol (e.g. PLTR) |
| `TO`     | The original symbol to restore (e.g. AAPL) |
| `QTY`    | Shares to buy back. Auto-filled if `/rotate` was used |

**Examples:**
```
/restore PLTR AAPL          QTY loaded from saved rotation
/restore PLTR AAPL 50       explicit qty (overrides saved record)
```

Before executing, the CLI checks whether proceeds cover the full restoration:

- **Surplus** — shows extra shares or leftover cash  
- **Shortfall** — shows how many shares you can actually afford and asks whether to proceed with that reduced qty

---

### `/rotations`

List all open rotation records.

```
  ────────────────────────────────────────────────────
     Temp   Restore     Qty   Sell@   Opened
  ────────────────────────────────────────────────────
     PLTR      AAPL      50  €148.30  2026-07-01 14:22
     → /restore PLTR AAPL 50
  ────────────────────────────────────────────────────
```

---

## Full Example Walkthrough

```
# You hold 100 AAPL. You want to trade PLTR short-term.

/rotate AAPL 50 PLTR +10
  ⏳ Previewing SELL 50 AAPL…
  [preview shown]
  ➜  Will BUY PLTR with ≈€7,415.00 proceeds
  Restore plan:  /restore PLTR AAPL 50
  Take-profit:   will place +10.0% limit on PLTR

  Confirm: SELL 50 × AAPL then BUY PLTR? [y/N] › y
  ✅ SELL filled — 50 × AAPL @ €148.30  proceeds ≈€7,415.00
  ✅ BUY filled — 312 × PLTR @ €23.76
  ✅ Take-profit placed — ID: 9912847

# Two days later, PLTR TP fills. Now restore.

/rotations
  → /restore PLTR AAPL 50

/restore PLTR AAPL
  Rotation record found: restore 50 × AAPL (opened 2026-07-01 14:22)
  ⏳ Previewing SELL 312 PLTR (all)…
  [preview shown]
  Restore plan:  buy back 50 × AAPL @ ≈€148.30
  Estimated cost: €7,415.00  |  proceeds: ≈€8,157.00
  ✅ Proceeds cover restoration  (surplus ≈€742.00 = 5 extra AAPL shares)

  Confirm: SELL 312 × PLTR  then BUY 50 × AAPL? [y/N] › y
  ✅ SELL filled — 312 × PLTR @ €26.14  proceeds €8,155.68
  ✅ BUY filled — 50 × AAPL @ €148.30
     Leftover cash: €740.68

  Rotation complete:  PLTR → AAPL (50 shares restored)
```

Net result: 100 AAPL still held (50 unchanged + 50 restored), plus €740 profit extracted.

---

## State File

Rotations are saved in `data/rotations.json`:

```json
{
  "PLTR": {
    "restore_symbol": "AAPL",
    "restore_qty": 50,
    "sell_price": 148.30,
    "temp_qty": 312,
    "opened_at": "2026-07-01 14:22"
  }
}
```

The record is **removed automatically** when `/restore` completes successfully. If you close the temp position manually (outside the CLI), remove the stale record by editing the file directly.

---

## Edge Cases

| Situation | Behaviour |
|-----------|-----------|
| Proceeds fall short of full restoration | Shows affordable qty, asks whether to proceed with reduced qty |
| BUY leg fails after SELL fills | Proceeds stay as cash; CLI prints the exact `/buy` command to run manually |
| Take-profit not set via `+X%` | Set it later with `/sell TO all @PRICE limit` |
| Multiple open rotations | Each is keyed by the temp symbol; `/rotations` lists all |
| Explicit QTY passed to `/restore` | Overrides the saved record (useful for partial restoration) |

---

## Related Commands

| Command | Description |
|---------|-------------|
| `/move FROM SIZE TO` | One-way move: sell FROM, buy TO (no tracking, no restore step) |
| `/sell TO all @PRICE limit` | Manually place a take-profit on the temp position |
| `/positions` | Check current holdings before rotating |
| `/q SYMBOL` | Live quote to estimate proceeds before committing |
