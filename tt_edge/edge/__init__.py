"""Edge math: de-vig (:mod:`vig`), THE edge calculation (:mod:`edge_calc`),
and staking (:mod:`staking`). ``edge_calc`` is the only place edge is
computed — everything else imports from it (the Q15 audit found three
inconsistent edge calcs; we do not repeat that here)."""
