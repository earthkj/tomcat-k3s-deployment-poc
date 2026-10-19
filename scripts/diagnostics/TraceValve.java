package local.poc;
import java.io.IOException;
import java.util.concurrent.atomic.AtomicInteger;
import jakarta.servlet.ServletException;
import org.apache.catalina.connector.Request;
import org.apache.catalina.connector.Response;
import org.apache.catalina.valves.ValveBase;

/** Diagnostic-only request lifecycle tracing; no payloads, keys or SQL are logged. */
public class TraceValve extends ValveBase {
    private static final AtomicInteger active = new AtomicInteger();
    private final String instance = System.getenv().getOrDefault("POC_INSTANCE", "unknown");
    public void invoke(Request request, Response response) throws IOException, ServletException {
        if (!request.getRequestURI().startsWith("/api/")) { getNext().invoke(request,response); return; }
        String id = request.getHeader("X-Request-ID");
        if (id == null || !id.matches("[A-Za-z0-9-]{1,100}")) id = "unknown";
        long started = System.nanoTime();
        int now = active.incrementAndGet();
        System.out.println("POC_TRACE event=BEGIN wall_ms="+System.currentTimeMillis()+" id="+id+" instance="+instance+" active="+now);
        try {
            // Only a marked probe on the kill target is held to guarantee an observed in-flight request.
            if (id.startsWith("probe-") && instance.equals("tomcat1")) {
                try { Thread.sleep(2500); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            }
            getNext().invoke(request,response);
        } finally {
            System.out.println("POC_TRACE event=END wall_ms="+System.currentTimeMillis()+" id="+id+" instance="+instance+" active="+active.decrementAndGet()+" status="+response.getStatus()+" elapsed_ms="+((System.nanoTime()-started)/1000000.0));
        }
    }
}
