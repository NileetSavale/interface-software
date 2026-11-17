# Native window, real OS input

The artifact Claude recorded on the canvas client (`artifacts/core_banking_gui.lookup_member_balance.json`), replayed with no model on a
native Tk window (`apps/mock_gui/desktop.py`): the screen read by OCR from a screen capture, the mouse and keyboard
driven through the operating system. Same artifact, a different renderer: the Tk window draws the same screens with
its own fonts, at the display's own scaling. `demo.mp4` is the window's rectangle only, recorded while it ran.

| shows | bank | result | drift | time | run |
|---|---|---|---|---|---|
| a member's balance, bank A | A | `success` | – | 44.4 s | `replay_20261117T020013Z_c87353` |
| a member number that doesn't exist | A | `business_outcome/MEMBER_NOT_FOUND` | – | 36.0 s | `replay_20261117T020058Z_5304ac` |
| the same lookup at bank B: new labels, font and colours | B | `success+drift` | enter_member_number:bbox, search:bbox | 45.5 s | `replay_20261117T020134Z_e214cf` |
| the host shows someone else's record | A | `failed/WRONG_RECORD` | – | 40.1 s | `replay_20261117T020219Z_19460b` |

The video shows the mock's fake members unmasked; each run's evidence screenshots (under `runs/`) black out every
value. Regenerate with `uv run python scripts/desktop_demo.py` (macOS, with Screen Recording and Accessibility
permission for the terminal).
