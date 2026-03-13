#!/bin/bash
# Use slim requirements on Vercel to stay under 500MB Lambda limit
if [ -n "$VERCEL" ]; then
  pip install -r webapp/requirements-vercel.txt
else
  pip install -r webapp/requirements.txt
fi
cd webapp && python manage.py migrate --noinput && python manage.py shell -c "from django.contrib.sites.models import Site; Site.objects.update_or_create(id=1, defaults={'domain': 'clear25.xyz', 'name': 'C.L.E.A.R.'})" && python manage.py collectstatic --noinput
