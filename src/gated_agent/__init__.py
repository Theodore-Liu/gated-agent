"""gated-agent: an options agent that red-teams its own risk exposure before every order.

Clean-room code for the Alpaca AI Trading Agents Hackathon. The signal layer is a
deliberately simple public-literature toy (Faber's 10-month SMA trend rule); the
discipline around it — options mapping, risk gates, red-team veto loop, negative
control, append-only decision log — is the point.
"""

__version__ = "0.1.0"
