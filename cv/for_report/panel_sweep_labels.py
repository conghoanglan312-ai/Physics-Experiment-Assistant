# -*- coding: utf-8 -*-
"""What the panel actually showed, read off the crops by eye.

Sampled every 20th crop of panel_sweep_20260921_211149 (one every 10 s) and read
from three montages. These are the ground truth everything else is measured
against -- no reader was involved in producing them.
"""
SWEEP = (r"C:\Users\LAN\Documents\GitHub\Physics-Experiment-Assistant"
         r"\test\output\panel_sweep_20260921_211149")
STEP = 20

LABELS = [
    "0090", "0000", "0012", "0012", "0034", "0034", "0042", "0056", "0056",
    "0076", "0078", "0078", "0078",
    "0090", "0093", "0123", "0131", "0145", "0162", "0167", "0186", "0189",
    "0201", "0201", "0249", "0250",
    "0320", "0309", "0309", "0367", "0367", "0382", "0529", "0458", "0456",
    "0623", "0623", "0623", "0623",
]
