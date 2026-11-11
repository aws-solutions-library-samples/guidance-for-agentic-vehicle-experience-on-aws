"""Simulated fleet operator — pure selection and planning (spec D10, prompt 276).

Packages:
    config   — parse SSM JSON configuration (D10a).
    select   — candidate filter and class assignment (D10b).
    pairing  — ground-and-book pairing logic (D10b, ported from CMS spec D1).
    plan     — work units, eligibility and cap planning (D10c/D10d).
"""
