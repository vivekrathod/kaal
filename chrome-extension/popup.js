const generalButton = document.querySelector("#save-general");
const medicalButton = document.querySelector("#save-medical");
const dashboardButton = document.querySelector("#open-dashboard");
const state = document.querySelector("#state");

function setBusy(busy) {
  generalButton.disabled = busy;
  medicalButton.disabled = busy;
  dashboardButton.disabled = busy;
}

async function capture(mode) {
  setBusy(true);
  state.classList.remove("error");
  state.textContent = mode === "medical" ? "Saving medical receipt…" : "Saving to Kaal…";
  try {
    const response = await chrome.runtime.sendMessage({ action: "capture-current-tab", mode });
    if (!response?.ok) throw new Error(response?.error || "Kaal capture failed");
    state.textContent = mode === "medical" ? "Saved as medical receipt." : "Saved to Kaal.";
  } catch (error) {
    state.classList.add("error");
    state.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    setBusy(false);
  }
}

generalButton.addEventListener("click", () => capture("general"));
medicalButton.addEventListener("click", () => capture("medical"));
dashboardButton.addEventListener("click", async () => {
  setBusy(true);
  try {
    const response = await chrome.runtime.sendMessage({ action: "open-dashboard" });
    if (!response?.ok) throw new Error(response?.error || "Could not open the Kaal dashboard");
    window.close();
  } catch (error) {
    state.classList.add("error");
    state.textContent = error instanceof Error ? error.message : String(error);
    setBusy(false);
  }
});
