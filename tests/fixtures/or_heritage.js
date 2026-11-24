const GOLD_MARGINS = {
  24: 0.9415635145223173,
  22: 0.8414784465547474,
  18: 0.6565858231627923,
  14: 0.47646742244058266,
  9:  0.30650509539764936
};
async function fetchGoldSpot() {
  const r = await fetch('/.netlify/functions/cours-or-spot', { cache: 'no-store' });
  const d = await r.json();
  if (d && typeof d.eur_g === 'number' && d.eur_g > 50 && d.eur_g < 500 && typeof d.quote_time === 'number') {
    LIVE_SPOT_24K = d.eur_g;
  }
}
function priceGold(carats) {
  return LIVE_SPOT_24K * (GOLD_MARGINS[carats] || 1);
}
