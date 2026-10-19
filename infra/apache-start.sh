#!/bin/sh
set -eu
mkdir -p /run/poc
oldIFS=$IFS
if [ ! -f /run/poc/workers.properties ]; then
cat > /run/poc/workers.properties <<EOF
worker.list=balancer,status
worker.balancer.type=lb
worker.balancer.balance_workers=${POC_WORKERS}
worker.balancer.method=Request
worker.status.type=status
EOF
oldIFS=$IFS
IFS=,
for worker in $POC_WORKERS; do
cat >> /run/poc/workers.properties <<EOF
worker.$worker.type=ajp13
worker.$worker.host=$worker
worker.$worker.port=${POC_AJP_PORT:-8009}
worker.$worker.secret=$POC_AJP_SECRET
worker.$worker.lbfactor=1
worker.$worker.ping_mode=A
worker.$worker.connection_ping_interval=10000
worker.$worker.socket_keepalive=true
worker.$worker.socket_connect_timeout=2000
worker.$worker.socket_timeout=35
worker.$worker.reply_timeout=35000
EOF
case ",${POC_STOPPED:-}," in
  *",$worker,"*) echo "worker.$worker.activation=Stopped" >> /run/poc/workers.properties ;;
esac
done
IFS=$oldIFS
chmod 600 /run/poc/workers.properties
fi
if [ "${POC_PROXY_KIND:-jk}" != jk ]; then
  cat > /etc/apache2/sites-enabled/poc.conf <<EOF
<VirtualHost *:80>
  ProxyRequests Off
  ProxyPreserveHost On
EOF
  if [ "$POC_PROXY_KIND" = http ]; then
    echo '  ProxyHCExpr ready200 {%{REQUEST_STATUS} == 200}' >> /etc/apache2/sites-enabled/poc.conf
  fi
  echo '  <Proxy balancer://poc>' >> /etc/apache2/sites-enabled/poc.conf
  IFS=,
  for worker in $POC_WORKERS; do
    if [ "$POC_PROXY_KIND" = http ]; then
      echo "    BalancerMember http://$worker:8080 loadfactor=1 connectiontimeout=2 timeout=35 retry=5 keepalive=On hcmethod=GET hcuri=/health/ready hcinterval=1 hcfails=1 hcpasses=1 hcexpr=ready200" >> /etc/apache2/sites-enabled/poc.conf
    else
      echo "    BalancerMember ajp://$worker:8009 loadfactor=1 connectiontimeout=2 timeout=35 retry=5 keepalive=On ping=10 secret=$POC_AJP_SECRET" >> /etc/apache2/sites-enabled/poc.conf
    fi
  done
  IFS=$oldIFS
  cat >> /etc/apache2/sites-enabled/poc.conf <<'EOF'
    ProxySet lbmethod=byrequests maxattempts=1 forcerecovery=Off
  </Proxy>
  ProxyPass / balancer://poc/
  ProxyPassReverse / balancer://poc/
  LogFormat "%t %>s %D %{X-Request-ID}i %{BALANCER_WORKER_NAME}e" poc
  CustomLog /proc/self/fd/1 poc
</VirtualHost>
EOF
fi
set +u
. /etc/apache2/envvars
set -u
mkdir -p "$APACHE_RUN_DIR" "$APACHE_LOCK_DIR"
exec apache2 -D FOREGROUND
