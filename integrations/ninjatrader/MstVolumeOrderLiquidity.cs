//
// MST Volume + Order + Liquidity  —  NinjaTrader 8 indicator
//
// Draws on the price panel:
//   * Order blocks (demand / supply zones) with live counters in each zone
//   * Resting-liquidity pools (equal highs / equal lows not yet traded through)
//   * Support and resistance levels, the strongest marked MAJOR
//
// What an order block is here: the last opposing candle before price CLOSED
// through the prior confirmed swing high or low. The zone is that candle's
// range. A zone is removed once price closes through its far side.
//
// The counters in each zone:
//   T   touches  - separate returns into the zone after it was confirmed
//   B   bars     - bars whose range overlapped the zone
//   O   orders   - executed trades (prints) inside the zone, from the tape
//   Buy / Sell   - volume that lifted the ask / hit the bid inside the zone
//   D   delta    - buy minus sell
//
// Orders, Buy and Sell come from the trade tape (OnMarketData). On historical
// bars the tape is only replayed when "Tick Replay" is enabled for the data
// series; without it those counters start at the moment the chart goes live,
// and the label falls back to volume estimated from the bars (marked "est").
// These are executed trades, not resting limit orders.
//
// Install: copy this file to
//   Documents\NinjaTrader 8\bin\Custom\Indicators\
// then open New > NinjaScript Editor and press F5 to compile. Add it to a chart
// from Indicators > MstVolumeOrderLiquidity.
//
#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class MstVolumeOrderLiquidity : Indicator
    {
        #region Internal types

        private class Zone
        {
            public int Id;
            public bool Bullish;
            public double Bottom;
            public double Top;
            public int StartBar;        // bar index of the block candle
            public int ConfirmedBar;    // bar index of the close that broke structure
            public int Touches;
            public int BarsInZone;
            public bool InsidePrev;
            public double EstVolume;    // bar volume prorated into the zone
            public double EstBuy;
            public double EstSell;
            public long TapeOrders;     // prints inside the zone
            public long TapeBuy;
            public long TapeSell;
            public bool Broken;
        }

        private class Pivot
        {
            public double Price;
            public int Bar;
            public double Volume;
        }

        private class Level
        {
            public double Price;
            public int Touches;
            public double Score;
            public int FirstBar;
            public int LastBar;
            public bool Major;
        }

        #endregion

        private readonly List<Zone> zones = new List<Zone>();
        private readonly List<Pivot> pivotHighs = new List<Pivot>();
        private readonly List<Pivot> pivotLows = new List<Pivot>();
        private readonly List<string> levelTags = new List<string>();

        private Pivot swingHigh;        // latest confirmed swing high not yet broken
        private Pivot swingLow;
        private int nextZoneId = 1;
        private ATR atr;
        private SMA avgVolume;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Order blocks with per-zone order counters, liquidity pools, and support / major resistance levels.";
                Name = "MstVolumeOrderLiquidity";
                Calculate = Calculate.OnBarClose;
                IsOverlay = true;
                DisplayInDataBox = false;
                DrawOnPricePanel = true;
                PaintPriceMarkers = false;
                IsSuspendedWhileInactive = true;

                SwingStrength = 3;
                MaxZones = 6;
                LookbackBars = 300;
                ZoneOpacity = 18;
                ShowOrderBlocks = true;
                ShowLevels = true;
                ShowLiquidity = true;
                MaxLevelsPerSide = 4;
            }
            else if (State == State.DataLoaded)
            {
                atr = ATR(14);
                avgVolume = SMA(Volume, 20);
            }
        }

        // ------------------------------------------------------------------
        // Tape: real executed trades inside each zone
        // ------------------------------------------------------------------
        protected override void OnMarketData(MarketDataEventArgs e)
        {
            if (e.MarketDataType != MarketDataType.Last || zones.Count == 0)
                return;

            for (int i = 0; i < zones.Count; i++)
            {
                Zone z = zones[i];
                if (z.Broken || e.Price < z.Bottom || e.Price > z.Top)
                    continue;

                z.TapeOrders++;
                if (e.Ask > 0 && e.Price >= e.Ask)
                    z.TapeBuy += e.Volume;
                else if (e.Bid > 0 && e.Price <= e.Bid)
                    z.TapeSell += e.Volume;
            }
        }

        // ------------------------------------------------------------------
        // Bars
        // ------------------------------------------------------------------
        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0)
                return;

            int k = SwingStrength;
            if (CurrentBar < Math.Max(2 * k + 1, 20))
                return;

            ConfirmPivots(k);
            UpdateZones();
            DetectBreaks();

            // Drawing every historical bar would rebuild the same objects
            // thousands of times; only the final state is ever looked at.
            if (State == State.Realtime || CurrentBar >= Bars.Count - 2)
                Render();
        }

        /// A pivot needs k bars on both sides, so the bar k bars ago is the
        /// newest one that can be confirmed on this bar.
        private void ConfirmPivots(int k)
        {
            bool isHigh = true;
            bool isLow = true;
            double h = High[k];
            double l = Low[k];

            for (int i = 0; i <= 2 * k; i++)
            {
                if (i == k)
                    continue;
                if (High[i] > h)
                    isHigh = false;
                if (Low[i] < l)
                    isLow = false;
            }

            if (isHigh)
            {
                Pivot p = new Pivot { Price = h, Bar = CurrentBar - k, Volume = Volume[k] };
                pivotHighs.Add(p);
                swingHigh = p;
            }
            if (isLow)
            {
                Pivot p = new Pivot { Price = l, Bar = CurrentBar - k, Volume = Volume[k] };
                pivotLows.Add(p);
                swingLow = p;
            }

            int oldest = CurrentBar - LookbackBars;
            pivotHighs.RemoveAll(p => p.Bar < oldest);
            pivotLows.RemoveAll(p => p.Bar < oldest);
        }

        /// A close through the latest confirmed swing is a break of structure;
        /// the last opposing candle before the breaking leg is the order block.
        private void DetectBreaks()
        {
            if (swingHigh != null && Close[0] > swingHigh.Price)
            {
                int span = CurrentBar - swingHigh.Bar;
                int origin = 0;
                for (int i = 1; i <= span; i++)
                    if (Low[i] < Low[origin])
                        origin = i;

                int limit = Math.Min(span, origin + 10);
                for (int i = Math.Max(origin, 1); i <= limit; i++)
                {
                    if (Close[i] < Open[i])
                    {
                        AddZone(true, i);
                        break;
                    }
                }
                swingHigh = null;
            }

            if (swingLow != null && Close[0] < swingLow.Price)
            {
                int span = CurrentBar - swingLow.Bar;
                int origin = 0;
                for (int i = 1; i <= span; i++)
                    if (High[i] > High[origin])
                        origin = i;

                int limit = Math.Min(span, origin + 10);
                for (int i = Math.Max(origin, 1); i <= limit; i++)
                {
                    if (Close[i] > Open[i])
                    {
                        AddZone(false, i);
                        break;
                    }
                }
                swingLow = null;
            }
        }

        private void AddZone(bool bullish, int barsAgo)
        {
            double bottom = Low[barsAgo];
            double top = High[barsAgo];

            // A very tall candle makes a zone too wide to mean anything; keep
            // the wick-to-open half, the side price left from.
            if (top - bottom > 1.5 * atr[Math.Min(barsAgo, 200)])
            {
                if (bullish)
                    top = Open[barsAgo];
                else
                    bottom = Open[barsAgo];
            }
            if (top <= bottom)
                return;

            Zone z = new Zone
            {
                Id = nextZoneId++,
                Bullish = bullish,
                Bottom = bottom,
                Top = top,
                StartBar = CurrentBar - barsAgo,
                ConfirmedBar = CurrentBar,
                InsidePrev = Low[0] <= top && High[0] >= bottom
            };

            // Count the bars from the block candle up to the confirming bar.
            for (int i = barsAgo; i >= 0; i--)
                Accumulate(z, i);

            // A newer zone replaces an older one it overlaps on the same side.
            for (int i = zones.Count - 1; i >= 0; i--)
            {
                Zone o = zones[i];
                if (o.Bullish == bullish && o.Bottom <= top && o.Top >= bottom)
                {
                    RemoveZoneDrawings(o);
                    zones.RemoveAt(i);
                }
            }

            zones.Add(z);
            while (zones.Count > MaxZones * 3)
            {
                RemoveZoneDrawings(zones[0]);
                zones.RemoveAt(0);
            }
        }

        /// Adds one bar's share of volume to a zone. Returns true if the bar
        /// traded inside the zone at all.
        private bool Accumulate(Zone z, int barsAgo)
        {
            double h = High[barsAgo];
            double l = Low[barsAgo];
            double span = h - l;
            double frac;

            if (span <= 0)
                frac = (Close[barsAgo] >= z.Bottom && Close[barsAgo] <= z.Top) ? 1.0 : 0.0;
            else
                frac = Math.Max(0.0, Math.Min(h, z.Top) - Math.Max(l, z.Bottom)) / span;

            if (frac <= 0)
                return false;

            double buyShare = span <= 0 ? 0.5 : (Close[barsAgo] - l) / span;
            double v = Volume[barsAgo] * frac;
            z.BarsInZone++;
            z.EstVolume += v;
            z.EstBuy += v * buyShare;
            z.EstSell += v * (1.0 - buyShare);
            return true;
        }

        private void UpdateZones()
        {
            for (int i = zones.Count - 1; i >= 0; i--)
            {
                Zone z = zones[i];
                if (CurrentBar <= z.ConfirmedBar)
                    continue;

                bool inside = Accumulate(z, 0);
                if (inside && !z.InsidePrev)
                    z.Touches++;
                z.InsidePrev = inside;

                // A close through the far side ends the zone; a wick does not.
                if ((z.Bullish && Close[0] < z.Bottom) || (!z.Bullish && Close[0] > z.Top))
                {
                    z.Broken = true;
                    RemoveZoneDrawings(z);
                    zones.RemoveAt(i);
                }
            }
        }

        // ------------------------------------------------------------------
        // Drawing
        // ------------------------------------------------------------------
        private void Render()
        {
            RenderZones();
            RenderLevelsAndPools();
        }

        private void RemoveZoneDrawings(Zone z)
        {
            RemoveDrawObject("mstOB" + z.Id);
            RemoveDrawObject("mstOBt" + z.Id);
        }

        private void RenderZones()
        {
            // Nearest zones to price first; the rest stay tracked but hidden.
            List<Zone> ordered = new List<Zone>(zones);
            double px = Close[0];
            ordered.Sort((a, b) =>
                Math.Abs((a.Top + a.Bottom) / 2 - px).CompareTo(Math.Abs((b.Top + b.Bottom) / 2 - px)));

            for (int i = 0; i < ordered.Count; i++)
            {
                Zone z = ordered[i];
                if (!ShowOrderBlocks || i >= MaxZones)
                {
                    RemoveZoneDrawings(z);
                    continue;
                }

                Brush brush = z.Bullish ? Brushes.MediumSeaGreen : Brushes.IndianRed;
                int startAgo = Math.Min(CurrentBar, CurrentBar - z.StartBar);
                Draw.Rectangle(this, "mstOB" + z.Id, false, startAgo, z.Top, 0, z.Bottom,
                    brush, brush, ZoneOpacity);

                Draw.Text(this, "mstOBt" + z.Id, ZoneLabel(z), Math.Min(startAgo, 6),
                    (z.Top + z.Bottom) / 2, Brushes.White);
            }
        }

        private string ZoneLabel(Zone z)
        {
            string side = z.Bullish ? "DEMAND" : "SUPPLY";
            if (z.TapeOrders > 0)
            {
                return string.Format("{0}  T:{1}  B:{2}  O:{3}  Buy:{4}  Sell:{5}  D:{6}",
                    side, z.Touches, z.BarsInZone, Compact(z.TapeOrders),
                    Compact(z.TapeBuy), Compact(z.TapeSell), Signed(z.TapeBuy - z.TapeSell));
            }
            return string.Format("{0}  T:{1}  B:{2}  Vol:{3}  Buy:{4}  Sell:{5} est",
                side, z.Touches, z.BarsInZone, Compact(z.EstVolume),
                Compact(z.EstBuy), Compact(z.EstSell));
        }

        private void RenderLevelsAndPools()
        {
            for (int i = 0; i < levelTags.Count; i++)
                RemoveDrawObject(levelTags[i]);
            levelTags.Clear();

            double atrNow = atr[0];
            if (atrNow <= 0)
                return;
            double px = Close[0];

            if (ShowLevels)
            {
                List<Pivot> all = new List<Pivot>(pivotHighs);
                all.AddRange(pivotLows);
                List<Level> levels = Cluster(all, 0.6 * atrNow, false, false);
                MarkMajor(levels, px);

                int res = 0, sup = 0;
                levels.Sort((a, b) => Math.Abs(a.Price - px).CompareTo(Math.Abs(b.Price - px)));
                for (int i = 0; i < levels.Count; i++)
                {
                    Level lv = levels[i];
                    bool support = lv.Price < px;
                    if (!lv.Major)
                    {
                        if (support && sup >= MaxLevelsPerSide) continue;
                        if (!support && res >= MaxLevelsPerSide) continue;
                    }
                    if (support) sup++; else res++;

                    Brush brush = support ? Brushes.LimeGreen : Brushes.Tomato;
                    string tag = "mstLV" + i;
                    int startAgo = Math.Min(CurrentBar, CurrentBar - lv.FirstBar);
                    Draw.Line(this, tag, false, startAgo, lv.Price, 0, lv.Price, brush,
                        DashStyleHelper.Solid, lv.Major ? 3 : 1);
                    levelTags.Add(tag);

                    string text = string.Format("{0}{1} {2}  x{3}",
                        lv.Major ? "MAJOR " : "", support ? "SUPPORT" : "RESISTANCE",
                        Instrument.MasterInstrument.FormatPrice(lv.Price), lv.Touches);
                    Draw.Text(this, tag + "t", text, Math.Min(startAgo, 22),
                        lv.Price + (support ? -0.25 : 0.25) * atrNow, brush);
                    levelTags.Add(tag + "t");
                }
            }

            if (ShowLiquidity)
            {
                DrawPools(Cluster(pivotHighs, 0.25 * atrNow, true, true), true, atrNow);
                DrawPools(Cluster(pivotLows, 0.25 * atrNow, true, false), false, atrNow);
            }
        }

        private void DrawPools(List<Level> pools, bool buySide, double atrNow)
        {
            for (int i = 0; i < pools.Count; i++)
            {
                Level p = pools[i];
                if (p.Touches < 2 || Swept(p, buySide))
                    continue;

                string tag = (buySide ? "mstBSL" : "mstSSL") + i;
                int startAgo = Math.Min(CurrentBar, CurrentBar - p.FirstBar);
                Draw.Line(this, tag, false, startAgo, p.Price, 0, p.Price, Brushes.Gold,
                    DashStyleHelper.Dash, 2);
                levelTags.Add(tag);

                string text = string.Format("{0} LIQUIDITY x{1}", buySide ? "BUY-SIDE" : "SELL-SIDE", p.Touches);
                Draw.Text(this, tag + "t", text, Math.Min(startAgo, 40),
                    p.Price + (buySide ? 0.25 : -0.25) * atrNow, Brushes.Gold);
                levelTags.Add(tag + "t");
            }
        }

        /// True once any bar after the last pivot traded through the pool.
        private bool Swept(Level p, bool buySide)
        {
            int bars = CurrentBar - p.LastBar;
            for (int i = 0; i < bars; i++)
            {
                if (buySide && High[i] > p.Price) return true;
                if (!buySide && Low[i] < p.Price) return true;
            }
            return false;
        }

        /// Groups pivots into bands no wider than `tolerance`, measured from the
        /// band's lowest pivot so a staircase of pivots cannot chain into one
        /// level. With `extreme`, the level sits at the band's outer edge
        /// (highest high / lowest low) rather than its average.
        private List<Level> Cluster(List<Pivot> pivots, double tolerance, bool extreme, bool useMax)
        {
            List<Pivot> sorted = new List<Pivot>(pivots);
            sorted.Sort((a, b) => a.Price.CompareTo(b.Price));

            List<Level> output = new List<Level>();
            double avgVol = avgVolume[0];
            double bandStart = 0;
            double sum = 0;
            Level cur = null;

            for (int i = 0; i < sorted.Count; i++)
            {
                Pivot p = sorted[i];
                if (cur == null || p.Price - bandStart > tolerance)
                {
                    cur = new Level { Price = p.Price, FirstBar = p.Bar, LastBar = p.Bar };
                    output.Add(cur);
                    bandStart = p.Price;
                    sum = 0;
                }

                cur.Touches++;
                sum += p.Price;
                cur.FirstBar = Math.Min(cur.FirstBar, p.Bar);
                cur.LastBar = Math.Max(cur.LastBar, p.Bar);

                if (extreme)
                    cur.Price = useMax ? Math.Max(cur.Price, p.Price) : Math.Min(cur.Price, p.Price);
                else
                    cur.Price = sum / cur.Touches;

                // A defence counts for more if it was recent and on heavy volume.
                double recency = 0.5 + 0.5 * Math.Max(0.0, 1.0 - (double)(CurrentBar - p.Bar) / Math.Max(1, LookbackBars));
                double weight = avgVol > 0 ? Math.Min(2.0, p.Volume / avgVol) : 1.0;
                cur.Score += recency * (0.5 + 0.5 * weight);
            }

            if (!extreme)
                output.RemoveAll(lv => lv.Touches < 2);
            return output;
        }

        /// Major = three or more touches and at least half the strongest
        /// level's score, capped at the two strongest on each side of price.
        private void MarkMajor(List<Level> levels, double px)
        {
            double top = 0;
            for (int i = 0; i < levels.Count; i++)
                top = Math.Max(top, levels[i].Score);

            for (int side = 0; side < 2; side++)
            {
                List<Level> eligible = new List<Level>();
                for (int i = 0; i < levels.Count; i++)
                {
                    Level lv = levels[i];
                    bool support = lv.Price < px;
                    if ((side == 0) == support && lv.Touches >= 3 && lv.Score >= 0.5 * top)
                        eligible.Add(lv);
                }
                eligible.Sort((a, b) => b.Score.CompareTo(a.Score));
                for (int i = 0; i < eligible.Count && i < 2; i++)
                    eligible[i].Major = true;
            }
        }

        private static string Compact(double v)
        {
            double a = Math.Abs(v);
            if (a >= 1e9) return (v / 1e9).ToString("0.00") + "B";
            if (a >= 1e6) return (v / 1e6).ToString("0.00") + "M";
            if (a >= 1e3) return (v / 1e3).ToString("0.0") + "K";
            return v.ToString("0");
        }

        private static string Signed(double v)
        {
            return (v >= 0 ? "+" : "") + Compact(v);
        }

        #region Properties

        [NinjaScriptProperty]
        [Range(2, 20)]
        [Display(Name = "Swing strength", Description = "Bars on each side that define a swing high / low.", Order = 1, GroupName = "Parameters")]
        public int SwingStrength { get; set; }

        [NinjaScriptProperty]
        [Range(1, 20)]
        [Display(Name = "Max zones shown", Description = "Order blocks drawn, nearest to price first.", Order = 2, GroupName = "Parameters")]
        public int MaxZones { get; set; }

        [NinjaScriptProperty]
        [Range(50, 5000)]
        [Display(Name = "Lookback bars", Description = "History used for support / resistance and liquidity pools.", Order = 3, GroupName = "Parameters")]
        public int LookbackBars { get; set; }

        [NinjaScriptProperty]
        [Range(1, 8)]
        [Display(Name = "Max levels per side", Description = "Non-major support / resistance levels drawn on each side of price.", Order = 4, GroupName = "Parameters")]
        public int MaxLevelsPerSide { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100)]
        [Display(Name = "Zone opacity", Order = 5, GroupName = "Parameters")]
        public int ZoneOpacity { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show order blocks", Order = 1, GroupName = "Layers")]
        public bool ShowOrderBlocks { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show support / resistance", Order = 2, GroupName = "Layers")]
        public bool ShowLevels { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show liquidity pools", Order = 3, GroupName = "Layers")]
        public bool ShowLiquidity { get; set; }

        #endregion
    }
}
