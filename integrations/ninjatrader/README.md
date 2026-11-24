# MST Volume + Order + Liquidity — NinjaTrader 8

`MstVolumeOrderLiquidity.cs` is the NinjaTrader chart version of the map on the
web app's Charting page: order blocks with counters in each zone, resting
liquidity pools, and support / resistance with the strongest marked MAJOR.

## Install

1. Copy `MstVolumeOrderLiquidity.cs` to
   `Documents\NinjaTrader 8\bin\Custom\Indicators\`.
2. In NinjaTrader: **New > NinjaScript Editor**, then press **F5** to compile.
3. On a chart: right-click > **Indicators** > add **MstVolumeOrderLiquidity**.

## Order counters need the tape

`O` (orders), `Buy`, `Sell` and `D` (delta) in each zone are counted from
executed trades. NinjaTrader only replays those on historical bars when
**Tick Replay** is on:

1. **Tools > Options > Market Data** > tick *Show Tick Replay*.
2. In the chart's **Data Series** dialog, tick *Tick Replay*.

Without it the counters start when the chart goes live, and until then each
zone shows volume estimated from the bars, marked `est`.

These are executed trades inside the zone, not resting limit orders.

## Status

Written against the documented NinjaScript API and syntax-checked against
stand-in types. It has not been compiled or run inside NinjaTrader itself.
