package local.poc;
import org.apache.catalina.*;
import org.apache.catalina.connector.Connector;
public class AjpDrainListener implements LifecycleListener {
    public void lifecycleEvent(LifecycleEvent event) {
        if (Lifecycle.BEFORE_STOP_EVENT.equals(event.getType()) && event.getLifecycle() instanceof Service service) {
            for (Connector connector : service.findConnectors()) {
                if (connector.getProtocolHandler().getClass().getName().contains("Ajp")) {
                    connector.pause();
                    System.out.println("POC_AJP_PAUSE " + System.currentTimeMillis());
                }
            }
        }
    }
}
