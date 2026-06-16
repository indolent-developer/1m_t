// Runs on Capital.com pages — extracts the currently selected instrument.

function detectInstrument() {
  // Try: chart title bar (e.g. "Super Micro Computer, Inc. KO · 1h")
  const chartTitle = document.querySelector(
    '[class*="chart-title"], [class*="instrument-title"], [data-qa="instrument-name"]'
  );

  // Try: the right-side order panel heading
  const panelName = document.querySelector(
    '[class*="deal-ticket"] [class*="instrument"], ' +
    '[class*="order-panel"] [class*="name"], ' +
    '[class*="trade-panel"] [class*="header"]'
  );

  const raw = (chartTitle || panelName)?.textContent?.trim() || "";

  // Extract epic from URL: /trading/EPIC or /chart/EPIC
  const urlMatch = location.pathname.match(/\/(?:trading|chart|markets?)\/([A-Z0-9_-]{2,20})/i);
  const epicFromUrl = urlMatch ? urlMatch[1].toUpperCase() : null;

  // Try to read bid/ask from the DOM
  const bidEl  = document.querySelector('[class*="bid"], [data-qa="bid"]');
  const askEl  = document.querySelector('[class*="ask"], [class*="offer"], [data-qa="ask"]');
  const bid    = parseFloat(bidEl?.textContent?.replace(/[^\d.]/g, "") || "0") || null;
  const ask    = parseFloat(askEl?.textContent?.replace(/[^\d.]/g, "") || "0") || null;

  // Try to read the currently selected KO level from the order panel
  const koEl   = document.querySelector(
    '[class*="knock-out"] input, [class*="knockout"] input, ' +
    '[data-qa="stop-level"] input, [placeholder*="knock"]'
  );
  const koLevel = parseFloat(koEl?.value || "0") || null;

  return { raw, epicFromUrl, bid, ask, koLevel };
}

// Send detection result whenever popup asks
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.action === "detectMarket") {
    sendResponse(detectInstrument());
  }
});

// Re-detect on SPA navigation
let lastUrl = location.href;
new MutationObserver(() => {
  if (location.href !== lastUrl) {
    lastUrl = location.href;
    chrome.runtime.sendMessage({ action: "pageNavigated", data: detectInstrument() });
  }
}).observe(document.body, { childList: true, subtree: true });
