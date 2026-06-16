// options.js

async function load() {
  const stored = await chrome.storage.sync.get(["apiKey", "username", "password", "isDemo"]);
  document.getElementById("apiKey").value   = stored.apiKey   || "";
  document.getElementById("username").value = stored.username || "";
  document.getElementById("password").value = stored.password || "";
  document.getElementById("isDemo").checked = stored.isDemo   !== false;
}

document.getElementById("saveBtn").addEventListener("click", async () => {
  const apiKey   = document.getElementById("apiKey").value.trim();
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;
  const isDemo   = document.getElementById("isDemo").checked;
  const status   = document.getElementById("status");

  if (!apiKey || !username || !password) {
    status.className = "status error";
    status.textContent = "All fields are required.";
    return;
  }

  await chrome.storage.sync.set({ apiKey, username, password, isDemo });
  status.className = "status";
  status.textContent = "Testing connection…";

  // Test via background
  chrome.runtime.sendMessage({ action: "ping" }, resp => {
    if (chrome.runtime.lastError || !resp?.ok) {
      status.className = "status error";
      status.textContent = "Connection failed: " + (resp?.error || chrome.runtime.lastError?.message);
    } else {
      status.className = "status ok";
      status.textContent = "Connected successfully!";
    }
  });
});

load();
