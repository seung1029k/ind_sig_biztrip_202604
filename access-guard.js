(function () {
  var STORAGE_KEY = "mas_trip_access_202604";
  var PASSWORD = "720929";
  var REDIRECT_PATH = "index.html";

  function tryGetSession(key) {
    try {
      return window.sessionStorage.getItem(key);
    } catch (error) {
      return null;
    }
  }

  function trySetSession(key, value) {
    try {
      window.sessionStorage.setItem(key, value);
    } catch (error) {
      return false;
    }
    return true;
  }

  function showDocument() {
    document.documentElement.style.visibility = "";
  }

  function redirectToSafePage() {
    try {
      if (window.self !== window.top) {
        window.top.location.replace(REDIRECT_PATH);
        return;
      }
    } catch (error) {
      // Fall back to the current frame when top navigation is blocked.
    }

    window.location.replace(REDIRECT_PATH);
  }

  document.documentElement.style.visibility = "hidden";

  if (tryGetSession(STORAGE_KEY) === "granted") {
    showDocument();
    return;
  }

  var entered = window.prompt("Password required", "");
  if (entered === PASSWORD) {
    trySetSession(STORAGE_KEY, "granted");
    showDocument();
    return;
  }

  window.alert("Access denied");
  redirectToSafePage();
})();
