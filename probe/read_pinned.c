#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>
#include <time.h>
#include <errno.h>
#include <string.h>

struct network_stats {
    unsigned long long pkts;
    unsigned long long bytes;
};

struct packet_loss_stats {
    unsigned long long received_packets;
    unsigned long long expected_packets;
};

struct stream_stats {
    unsigned long long frames;
    unsigned long long i_frames;
    unsigned long long p_frames;
    unsigned long long incomplete_frames;
    unsigned long long incomplete_i_frames;
    unsigned long long incomplete_p_frames;
};

static void write_metrics_json(const char *out_path,
                               unsigned long long pkts_per_s,
                               unsigned long long bytes_per_s,
                               double mbps,
                               unsigned long long frames_total,
                               unsigned long long frames_per_s,
                               unsigned long long i_total,
                               unsigned long long i_per_s,
                               unsigned long long p_total,
                               unsigned long long p_per_s,
                               unsigned long long incomplete_total,
                               unsigned long long incomplete_per_s,
                               unsigned long long incomplete_i_total,
                               unsigned long long incomplete_i_per_s,
                               unsigned long long incomplete_p_total,
                               unsigned long long incomplete_p_per_s,
                               unsigned long long received_packets_total,
                               unsigned long long expected_packets_total,
                               double             pkt_loss_pct,          
                               double             pkt_loss_pct_per_s)    
{
    char tmp_path[512];
    snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", out_path);

    FILE *f = fopen(tmp_path, "w");
    if (!f) {
        fprintf(stderr, "fopen(%s) failed: %s\n", tmp_path, strerror(errno));
        return;
    }

    time_t now = time(NULL);
    struct tm tm_local;
    localtime_r(&now, &tm_local);

    char iso_time[64];
    strftime(iso_time, sizeof(iso_time), "%Y-%m-%d %H:%M:%S", &tm_local);

    double bps = mbps * 1000000.0;

    fprintf(f,
        "{"
        "\"timestamp_sec\":%ld,"
        "\"timestamp_iso\":\"%s\","
        "\"throughput_bps\":%.0f,"
        "\"throughput_mbps\":%.3f,"
        "\"pkts_per_s\":%llu,"
        "\"bytes_per_s\":%llu,"
        "\"frames_total\":%llu,"
        "\"frames_per_s\":%llu,"
        "\"i_frames_total\":%llu,"
        "\"i_frames_per_s\":%llu,"
        "\"p_frames_total\":%llu,"
        "\"p_frames_per_s\":%llu,"
        "\"incomplete_frames_total\":%llu,"
        "\"incomplete_frames_per_s\":%llu,"
        "\"incomplete_i_frames_total\":%llu,"
        "\"incomplete_i_frames_per_s\":%llu,"
        "\"incomplete_p_frames_total\":%llu,"
        "\"incomplete_p_frames_per_s\":%llu,"
        "\"received_packets_total\":%llu,"  
        "\"expected_packets_total\":%llu,"     
        "\"lost_packets_total\":%llu,"         
        "\"pkt_loss_pct\":%.2f,"               
        "\"pkt_loss_pct_per_s\":%.2f"        
        "}\n",
        now, iso_time, bps, mbps,
        pkts_per_s, bytes_per_s,
        frames_total, frames_per_s,
        i_total, i_per_s,
        p_total, p_per_s,
        incomplete_total,   incomplete_per_s,
        incomplete_i_total, incomplete_i_per_s,
        incomplete_p_total, incomplete_p_per_s,
        received_packets_total,                                       
        expected_packets_total,                                       
        expected_packets_total - received_packets_total,               
        pkt_loss_pct,                                                  
        pkt_loss_pct_per_s                                            
    );

    fclose(f);

    if (rename(tmp_path, out_path) != 0) {
        fprintf(stderr, "rename(%s -> %s) failed: %s\n",
                tmp_path, out_path, strerror(errno));
    }
}

int main(int argc, char **argv)
{
    const char *pin_net_path    = "/sys/fs/bpf/stream/network_stats";
    const char *pin_stream_path = "/sys/fs/bpf/stream/stream_stats";
    const char *pin_loss_path = "/sys/fs/bpf/stream/packet_loss_stats";
    const char *out_path        = "metrics.json";

    if (argc >= 2) pin_net_path    = argv[1];
    if (argc >= 3) pin_stream_path = argv[2];
    if (argc >= 4) out_path        = argv[3];

    int net_fd = bpf_obj_get(pin_net_path);
    if (net_fd < 0) {
        fprintf(stderr, "bpf_obj_get(%s) failed: %s\n", pin_net_path, strerror(errno));
        return 1;
    }

    int stream_fd = bpf_obj_get(pin_stream_path);
    if (stream_fd < 0) {
        fprintf(stderr, "bpf_obj_get(%s) failed: %s\n", pin_stream_path, strerror(errno));
        return 1;
    }


    int loss_fd = bpf_obj_get(pin_loss_path);
    if (loss_fd < 0) {
        fprintf(stderr, "bpf_obj_get(%s) failed: %s\n", pin_loss_path, strerror(errno));
        return 1;
    }

    int ncpu = libbpf_num_possible_cpus();
    if (ncpu <= 0) ncpu = 1;

    unsigned int key = 0;

    struct packet_loss_stats pl = {};
    struct network_stats *net_percpu = calloc((size_t)ncpu, sizeof(struct network_stats));
    if (!net_percpu) {
        perror("calloc(net_percpu)");
        return 1;
    }

    struct stream_stats *stream_percpu = calloc((size_t)ncpu, sizeof(struct stream_stats));
    if (!stream_percpu) {
        perror("calloc(stream_percpu)");
        return 1;
    }

    unsigned long long last_pkts         = 0, last_bytes        = 0;
    unsigned long long last_frames       = 0, last_i            = 0;
    unsigned long long last_p            = 0;
    unsigned long long last_incomplete   = 0;
    unsigned long long last_incomplete_i = 0;
    unsigned long long last_incomplete_p = 0;
    unsigned long long last_received     = 0;
    unsigned long long last_expected     = 0;   

    while (1) {
        
        /* ----- Read PERCPU network ----- */
        unsigned long long total_pkts = 0, total_bytes = 0;

        if (bpf_map_lookup_elem(net_fd, &key, net_percpu) == 0) {
            for (int i = 0; i < ncpu; i++) {
                total_pkts  += net_percpu[i].pkts;
                total_bytes += net_percpu[i].bytes;
            }
        } else {
            fprintf(stderr, "bpf_map_lookup_elem(network_stats) failed: %s\n", strerror(errno));
        }


        /* ----- Read PERCPU stream stats ----- */
        unsigned long long frames_total       = 0, i_total           = 0;
        unsigned long long p_total            = 0;
        unsigned long long incomplete_total   = 0;
        unsigned long long incomplete_i_total = 0;
        unsigned long long incomplete_p_total = 0;
        unsigned long long received_total     = 0;  
        unsigned long long expected_total     = 0;   

        if (bpf_map_lookup_elem(stream_fd, &key, stream_percpu) == 0) {
            for (int i = 0; i < ncpu; i++) {
                frames_total       += stream_percpu[i].frames;
                i_total            += stream_percpu[i].i_frames;
                p_total            += stream_percpu[i].p_frames;
                incomplete_total   += stream_percpu[i].incomplete_frames;
                incomplete_i_total += stream_percpu[i].incomplete_i_frames;
                incomplete_p_total += stream_percpu[i].incomplete_p_frames;
            }
        } else {
            fprintf(stderr, "bpf_map_lookup_elem(stream_stats) failed: %s\n", strerror(errno));
        }

        if (bpf_map_lookup_elem(loss_fd, &key, &pl) == 0) {
            received_total = pl.received_packets;
            expected_total = pl.expected_packets;
        } else {
            fprintf(stderr, "bpf_map_lookup_elem(packet_loss_stats) failed: %s\n", strerror(errno));
        }

        /* ----- Compute per-second deltas ----- */
        unsigned long long d_pkts         = total_pkts         - last_pkts;
        unsigned long long d_bytes        = total_bytes        - last_bytes;
        unsigned long long d_frames       = frames_total       - last_frames;
        unsigned long long d_i            = i_total            - last_i;
        unsigned long long d_p            = p_total            - last_p;
        unsigned long long d_incomplete   = incomplete_total   - last_incomplete;
        unsigned long long d_incomplete_i = incomplete_i_total - last_incomplete_i;
        unsigned long long d_incomplete_p = incomplete_p_total - last_incomplete_p;
        unsigned long long d_received     = received_total     - last_received; 
        unsigned long long d_expected     = expected_total     - last_expected; 

        /* ----- Packet loss % ----- */
        double pkt_loss_pct = 0.0;                                              
        if (expected_total > 0) {
            unsigned long long lost = expected_total - received_total;
            pkt_loss_pct = (double)lost / (double)expected_total * 100.0;
        }

        double pkt_loss_pct_per_s = 0.0;                                        
        if (d_expected > 0) {
            unsigned long long lost_per_s = d_expected - d_received;
            pkt_loss_pct_per_s = (double)lost_per_s / (double)d_expected * 100.0;
        }

        double mbps = (d_bytes * 8.0) / 1e6;

        write_metrics_json(out_path,
                           d_pkts, d_bytes, mbps,
                           frames_total,       d_frames,
                           i_total,            d_i,
                           p_total,            d_p,
                           incomplete_total,   d_incomplete,
                           incomplete_i_total, d_incomplete_i,
                           incomplete_p_total, d_incomplete_p,
                           received_total,     expected_total,  
                           pkt_loss_pct,       pkt_loss_pct_per_s); 

        last_pkts         = total_pkts;
        last_bytes        = total_bytes;
        last_frames       = frames_total;
        last_i            = i_total;
        last_p            = p_total;
        last_incomplete   = incomplete_total;
        last_incomplete_i = incomplete_i_total;
        last_incomplete_p = incomplete_p_total;
        last_received     = received_total; 
        last_expected     = expected_total;  

        sleep(1);
    }
}