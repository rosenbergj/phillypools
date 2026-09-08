import { defineRailway, project, service } from "railway/iac";

// Last resort for a per-service CaC repo. Prefer one .railway file for the
// project and drop this if you later combine services into that file.
export const partial = "phillypools";

export default defineRailway(() => {
  const phillypools = service("phillypools", {
    build: "python manage.py collectstatic --noinput",
    start: "if [ \"$RAILWAY_SERVICE_NAME\" = \"url-watcher\" ]; then python manage.py run_url_watcher; else python manage.py migrate --noinput && gunicorn phillypools.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 120; fi",
    healthcheck: "/",
    healthcheckTimeout: 30,
  });
  return project("phillypools", {
    resources: [phillypools],
  });
});
