FROM maven:3.9.9-eclipse-temurin-21 AS build
WORKDIR /build
COPY pom.xml .
COPY src src
RUN mvn -B -ntp package
FROM tomcat:10.1.60-jdk21-temurin-noble@sha256:c2683584f9207cd05404b495675de73a2ad5a4953d4bd6f462a3fe0e53c1e530 AS runtime
COPY infra/AjpDrainListener.java /tmp/AjpDrainListener.java
RUN mkdir /tmp/classes && javac -cp /usr/local/tomcat/lib/catalina.jar:/usr/local/tomcat/lib/tomcat-coyote.jar:/usr/local/tomcat/lib/tomcat-util.jar -d /tmp/classes /tmp/AjpDrainListener.java && jar cf /usr/local/tomcat/lib/poc-drain.jar -C /tmp/classes . && rm -rf /tmp/classes /tmp/AjpDrainListener.java
COPY infra/server.xml /usr/local/tomcat/conf/server.xml
COPY --from=build /build/target/demo-api.war /opt/releases/demo-api.war
RUN mkdir -p /usr/local/tomcat/webapps/ROOT && cd /usr/local/tomcat/webapps/ROOT && jar xf /opt/releases/demo-api.war && chown -R 10001:10001 /usr/local/tomcat
ENV JAVA_OPTS="-Xms256m -Xmx512m -Dorg.apache.tomcat.util.digester.PROPERTY_SOURCE=org.apache.tomcat.util.digester.EnvironmentPropertySource"
USER 10001:10001
CMD ["catalina.sh", "run"]
