// An extension loaded from a folder keeps running the copy Chrome loaded, and
// its worker in particular, whatever the folder holds now. This tells the
// popup and the settings page when the folder has a different version, so they
// can offer to finish the update. Nothing here reloads by itself.

export async function updateWaiting() {
  try {
    const onDisk = await (await fetch(chrome.runtime.getURL("manifest.json"), { cache: "no-store" })).json();
    const running = chrome.runtime.getManifest().version;
    return onDisk.version && onDisk.version !== running ? { running, onDisk: onDisk.version } : null;
  } catch {
    return null;   // the folder could not be read: carry on with the copy that is running
  }
}

export const finishUpdate = () => chrome.runtime.reload();
