To ensure full alignment between your analytics guide and the actual dashboard implementation, here is the updated **XRPBurn.md**. This version precisely matches the hex colors, weekend markers, and data logic used in your `index.html` and `generate_data.py`.

---

# XRPBurn Institutional Analytics Guide

This document serves as the reference for interpreting the **XRPBurn Analysis Daily** dashboard. It aligns with the hourly "growing stack" logic and the institutional categorization of the XRP Ledger (XRPL).

## 📊 Dashboard Overview

The dashboard tracks two primary signals:

1. **Network Load (Top Chart):** The economic weight (USD Millions) moving through the ledger.
2. **Transaction Breakdown (Bottom Chart):** The raw volume (Millions) of ledger activity.

### 🎨 Color & Category Mapping

To maintain institutional clarity, both charts use a consistent color-coding scheme:

* 🟢 **Settlement (`#2ca02c`)**: Represents cross-currency payments and high-value FX tranches. This is the primary "Institutional Pulse" indicator.
* 🟣 **Identity (`#9467bd`)**: Represents DID (Decentralized Identity) handshakes and bank-grade onboarding activity (e.g., Standard Chartered/Zand-style credentials).
* 🟠 **DeFi (`#ff7f0e`)**: Represents Automated Market Maker (AMM) swaps and pathfinding activity.
* ⚪ **Account Management (`#7f7f7f`)**: Represents multi-sig configurations, wallet deletions, and governance-level operations.

---

## 📈 Key Indicators & Thresholds

### 1. The 1,000 XRP Burn Threshold

* **Visual:** A red dashed line on the top chart.
* **Significance:** This is the "Deflationary Trigger." Under normal efficiency, the ledger burns <500 XRP/day. If the burn line (Solid Red) crosses this threshold, it indicates network congestion or high-priority fee scaling, signaling a shift from "High Efficiency" to "High Urgency" settlement.

### 2. Weekend Markers (The Dot Legend)

Located at the bottom of each chart to identify volume shifts:

* 🔵 **Blue Dot (Saturday)**: Often identifies the "Retail Pulse" or testing phases.
* *Reference:* **The Zand Spike (Feb 21)**. A divergence where Transaction Count (Bottom) increases while Network Load (Top) decreases suggests high-volume, low-value retail or test transactions.


* 🔴 **Red Dot (Sunday)**: Represents the typical institutional lull before the Monday market overlap.

---

## 🕒 Data Execution Logic

### The "Growing Stack" (Hourly Updates)

The dashboard uses an incremental update system triggered via GitHub Actions:

* **Consolidation:** Data is consolidated on a **Singapore Time (SGT)** basis.
* **Intra-day Growth:** Between 12:00 AM and 11:59 PM SGT, the "Today" bar on the chart is not a final value. It grows 24 times throughout the day as hourly tranches are added.
* **Heartbeat:** The "Last Updated" timestamp on the dashboard confirms the exact hour of the current "stack" progress.

---

## 🔍 Institutional Analyst Notes

* **High-Quality Load:** A high ratio of **Settlement (Green)** and **Identity (Purple)** indicates "clean" institutional growth.
* **Efficiency Signal:** If Network Load (USD) rises but XRP Burn remains flat, the ledger is operating at maximum efficiency.
* **MSTR Correlation:** Use the 06:00 AM SGT daily rollover to assess how ledger activity aligns with Bitcoin/MSTR "Max Pain" options pricing for the upcoming session.
