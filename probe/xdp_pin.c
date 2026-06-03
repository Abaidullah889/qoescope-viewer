#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <net/if.h>
#include <stdio.h>
#include <unistd.h>
#include <sys/resource.h>
#include <errno.h>

static void bump_memlock(void)
{
    struct rlimit r = {RLIM_INFINITY, RLIM_INFINITY};
    setrlimit(RLIMIT_MEMLOCK, &r);
}

static int pin_map(struct bpf_object *obj, const char *map_name, const char *pin_path)
{
    int map_fd = bpf_object__find_map_fd_by_name(obj, map_name);
    if (map_fd < 0) {
        fprintf(stderr, "Map '%s' not found\n", map_name);
        return -1;
    }

    // Remove old pin if exists (avoid EEXIST)
    unlink(pin_path);

    if (bpf_obj_pin(map_fd, pin_path) < 0) {
        fprintf(stderr, "bpf_obj_pin(%s -> %s) failed: %s\n",
                map_name, pin_path, strerror(errno));
        return -1;
    }

    printf("Pinned map '%s' at: %s\n", map_name, pin_path);
    return 0;
}

int main(void)
{
    const char *iface     = "eth0";
    const char *obj_path  = "/data/xdp_rtp_count.o";
    const char *prog_name = "xdp_prog";

    const char *map_net_name    = "network_stats";
    const char *map_stream_name = "stream_stats";
    const char *map_loss_name   = "packet_loss_stats";

    const char *pin_net_path    = "/sys/fs/bpf/stream/network_stats";
    const char *pin_stream_path = "/sys/fs/bpf/stream/stream_stats";
    const char *pin_loss_path   = "/sys/fs/bpf/stream/packet_loss_stats";

    int xdp_flags = 2;

    bump_memlock();
    libbpf_set_strict_mode(LIBBPF_STRICT_ALL);

    int ifindex = if_nametoindex(iface);
    if (!ifindex) {
        perror("if_nametoindex");
        return 1;
    }

    struct bpf_object *obj = bpf_object__open_file(obj_path, NULL);
    if (!obj) {
        fprintf(stderr, "Failed to open BPF object: %s\n", obj_path);
        return 1;
    }

    if (bpf_object__load(obj)) {
        fprintf(stderr, "Failed to load BPF object\n");
        bpf_object__close(obj);
        return 1;
    }

    struct bpf_program *prog = bpf_object__find_program_by_name(obj, prog_name);
    if (!prog) {
        fprintf(stderr, "Program '%s' not found\n", prog_name);
        bpf_object__close(obj);
        return 1;
    }

    int prog_fd = bpf_program__fd(prog);
    if (prog_fd < 0) {
        fprintf(stderr, "Failed to get prog fd\n");
        bpf_object__close(obj);
        return 1;
    }

    if (bpf_set_link_xdp_fd(ifindex, prog_fd, xdp_flags) < 0) {
        perror("bpf_set_link_xdp_fd(attach)");
        bpf_object__close(obj);
        return 1;
    }

    printf("XDP attached on %s (mode=skb)\n", iface);
    if (pin_map(obj, map_net_name, pin_net_path) < 0) {
        bpf_set_link_xdp_fd(ifindex, -1, xdp_flags);
        bpf_object__close(obj);
        return 1;
    }

    if (pin_map(obj, map_stream_name, pin_stream_path) < 0) {
        bpf_set_link_xdp_fd(ifindex, -1, xdp_flags);
        bpf_object__close(obj);
        return 1;
    }

    if (pin_map(obj, map_loss_name, pin_loss_path) < 0) {
        bpf_set_link_xdp_fd(ifindex, -1, xdp_flags);
        bpf_object__close(obj);
        return 1;
    }

    bpf_object__close(obj);
    return 0;
}
