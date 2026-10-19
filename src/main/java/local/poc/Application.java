package local.poc;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Map;
import jakarta.servlet.*;
import jakarta.servlet.http.*;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.web.servlet.support.SpringBootServletInitializer;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.ResponseEntity;
import org.springframework.web.filter.OncePerRequestFilter;

@SpringBootApplication
public class Application extends SpringBootServletInitializer {
    @Override protected SpringApplicationBuilder configure(SpringApplicationBuilder b) { return b.sources(Application.class); }
    public static void main(String[] args) { SpringApplication.run(Application.class, args); }
}

@Component @Order(Ordered.HIGHEST_PRECEDENCE)
class Authentication extends OncePerRequestFilter {
    private final byte[] key = required("POC_API_KEY").getBytes(StandardCharsets.UTF_8);
    static String required(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) throw new IllegalStateException(name + " is required");
        return value;
    }
    @Override protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain) throws ServletException, IOException {
        req.setAttribute("started", System.nanoTime());
        String path = req.getRequestURI();
        if (!path.equals("/health/live") && !path.equals("/health/ready")) {
            String supplied = req.getHeader("X-API-Key");
            if (supplied == null || !MessageDigest.isEqual(key, supplied.getBytes(StandardCharsets.UTF_8))) {
                res.setStatus(401); res.setContentType("application/json"); res.getWriter().write("{\"error\":\"unauthorized\"}"); return;
            }
        }
        chain.doFilter(req, res);
    }
}

@RestController
class Api {
    private final JdbcTemplate jdbc;
    private final String version = System.getenv().getOrDefault("POC_VERSION", "v1");
    private final String instance = System.getenv().getOrDefault("POC_INSTANCE", System.getenv().getOrDefault("HOSTNAME", "unknown"));
    private final long processingMs = Long.parseLong(System.getenv().getOrDefault("POC_PROCESSING_MS", "100"));
    Api(JdbcTemplate jdbc) { this.jdbc = jdbc; }
    @GetMapping("/health/live") Map<String,Object> live() { return Map.of("status", "UP", "version", version, "instance", instance); }
    @GetMapping("/health/ready") ResponseEntity<?> ready() {
        try {
            Integer n = jdbc.queryForObject("SELECT count(*) FROM companies WHERE id=1", Integer.class);
            return ResponseEntity.status(n != null && n == 1 ? 200 : 503).body(Map.of("ready", n != null && n == 1, "version", version, "instance", instance));
        } catch (DataAccessException e) { return ResponseEntity.status(503).body(Map.of("ready", false)); }
    }
    @GetMapping("/api/companies/{id}") ResponseEntity<?> company(@PathVariable String id, HttpServletRequest req) throws InterruptedException {
        long number;
        try { number = Long.parseLong(id); if (number <= 0) throw new NumberFormatException(); }
        catch (NumberFormatException e) { return ResponseEntity.badRequest().body(Map.of("error", "invalid_id")); }
        try {
            var rows = jdbc.query("SELECT id,name,registration_number FROM companies WHERE id=?", (rs, i) -> Map.<String,Object>of("id",rs.getLong(1),"name",rs.getString(2),"registrationNumber",rs.getString(3)), number);
            if (rows.isEmpty()) return ResponseEntity.status(404).body(Map.of("error","not_found"));
            long started = (long)req.getAttribute("started");
            long remaining = processingMs * 1_000_000 - (System.nanoTime() - started);
            if (remaining > 0) java.util.concurrent.TimeUnit.NANOSECONDS.sleep(remaining);
            return ResponseEntity.ok(Map.of("company",rows.getFirst(),"version",version,"instance",instance,"serverProcessingMs",(System.nanoTime()-started)/1_000_000.0));
        } catch (DataAccessException e) { return ResponseEntity.status(503).body(Map.of("error","database_unavailable")); }
    }
}
