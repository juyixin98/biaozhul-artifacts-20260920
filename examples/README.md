# Example streams (three-level fork)

Genesis G:
  hash        0x09319c18cfc74f9de4add658322841f13cf7623977a75dcfbadc0beccf7ccea4
  transfer    alice -> bob 1000

Main/A fork (head until overtaken):
  A1 hash     0x94961a2c562bd572ccb63d86cfafce5ab5815064a35b82676c8c8fa9e12570c3  (bob -> carol 100)
  A2 hash     0x156e9d43ace17844cbd9c41795ad49aa8f8f245b93bf24ef457c46096f84afee  (carol -> alice 10)

B fork (final canonical chain; cumulative weight 4 wins):
  B1 hash     0xc822e7eff5988c47aab80bff1d4199dd16e2ec86b75354210dab56229768ee25  (bob -> dave 40)
  B2 hash     0xe4250a47c00c34b801eb5b4ddb5517521c89bb47749ad61cf8a7aa122641c095  (dave -> alice 5)
  B3 hash     0xa19bcc88141368692da29d5f9403084573a033ca37c39177f04aac489a607a70  (alice -> bob 200)

C fork (third level, branches off B1, ties A2/B2 at weight 3):
  C2 hash     0x0dab4cbeecec7c0c7679e1cb58320eb7ab2b81ecae320e909bf7e5b96e89d2ef  (bob -> carol 1)

Delivery order in stream1 is intentionally out of order: B3 (the future
height-3 tip) arrives BEFORE B2, so it is staged as an orphan and connected
automatically when B2 is delivered.

Final canonical chain balances after stream1 (G-B1-B2-B3):
  alice = -1000 (G) + 5 (B2) - 200 (B3) = -1195
  bob   = +1000 (G) - 40 (B1) + 200 (B3) = 1160
  dave  = +40 (B1) - 5 (B2)            = 35
  carol = 0 (her A1/A2 credits are reorged out; C2 never wins)
