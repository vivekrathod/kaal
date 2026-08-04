const detail = location.hash ? decodeURIComponent(location.hash.slice(1)) : "No error detail was provided.";
document.querySelector("#detail").textContent = detail;
