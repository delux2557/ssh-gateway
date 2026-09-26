# A deliberately hostile SSH target for the integration tests.
#
# Alpine + OpenSSH but *no bash*: every command the gateway runs has to work
# under the POSIX shell it picks itself, and detached jobs have to survive
# without bash-only job control. That is the shape of real targets this project
# is aimed at (containers, routers, recovery images).
FROM alpine:3.20

# A network-restricted build host can pass a regional mirror:
#   docker build --build-arg APK_MIRROR=mirror.example/internal/alpine ...
ARG APK_MIRROR=
RUN if [ -n "$APK_MIRROR" ]; then sed -i "s|dl-cdn.alpinelinux.org|$APK_MIRROR|g" /etc/apk/repositories; fi \
 && apk add --no-cache openssh openssh-sftp-server \
 && ssh-keygen -A \
 && adduser -D -h /home/testuser testuser \
 && echo 'testuser:test-password' | chpasswd \
 && mkdir -p /run/sshd /home/testuser/data \
 && printf 'hello from the target\n' > /home/testuser/data/hello.txt \
 && chown -R testuser:testuser /home/testuser \
 && printf '\nPort 2222\nPasswordAuthentication yes\nPermitRootLogin no\nSubsystem sftp /usr/lib/ssh/sftp-server\n' \
      >> /etc/ssh/sshd_config

EXPOSE 2222
CMD ["/usr/sbin/sshd", "-D", "-e", "-p", "2222"]
