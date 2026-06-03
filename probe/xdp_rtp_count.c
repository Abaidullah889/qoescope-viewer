#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/udp.h>
#include <linux/in.h>

#define DROP_EVERY 10

struct network_stats {
    __u64 pkts;
    __u64 bytes;
};

struct stream_stats {
    __u64 frames;     
    __u64 i_frames;   
    __u64 p_frames;
    __u64 incomplete_frames; 
    __u64 incomplete_i_frames;  
    __u64 incomplete_p_frames;

};

struct stream_state_shared {
    __u32 cur_ts;     // current frame RTP timestamp
    __u8  cur_type;   // 0=unknown, 1=P, 2=I
    __u32 ssrc; 
    __u8  seen_ts;          
    __u8  incomplete;      // gap detected in current frame?
    __u8  saw_marker;      // did current frame get its marker?
    __u16 last_seq;        // last seen sequence number
    __u8  pad[2];          // alignment
};

struct packet_loss_stats {
    __u64 received_packets;
    __u64 expected_packets;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct packet_loss_stats);
} packet_loss_stats SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct network_stats);
} network_stats SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct stream_stats);
} stream_stats SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct stream_state_shared);
} stream_state SEC(".maps");

struct rtp_hdr {
    __u8  vpxcc;
    __u8  mpt;
    __be16 sequence;
    __be32 timestamp;
    __be32 ssrc;
} __attribute__((packed));

static __always_inline int should_drop(__u16 seq)
{
    return (seq % DROP_EVERY) == 0;
}

static __always_inline int rtp_get_marker_ts_payload(void *rtp, void *data_end, __u8 *marker_out, __u32 *ts_out,__u16 *seq_out,__u32 *ssrc_out,__u8 **payload_out)
{
    struct rtp_hdr *h = (struct rtp_hdr *)rtp;

    /* Need fixed RTP header (12 bytes) */
    if ((void *)(h + 1) > data_end)
        return 0;

    /* RTP version must be 2 */
    __u8 vpxcc = h->vpxcc;
    __u8 version = (vpxcc >> 6) & 0x03;
    if (version != 2)
        return 0;

    /* Marker is top bit of mpt */
    *marker_out = (h->mpt >> 7) & 0x01;
    *ts_out = bpf_ntohl(h->timestamp);
    *seq_out    = bpf_ntohs(h->sequence);
    *ssrc_out = bpf_ntohl(h->ssrc);

    /* Start right after fixed header */
    __u8 *p = (__u8 *)(h + 1);

    /* Skip CSRC list if any (CC is low 4 bits) */
    __u8 cc = vpxcc & 0x0F;
    __u32 csrc_bytes = (__u32)cc * 4;
    if ((void *)(p + csrc_bytes) > data_end)
        return 0;
    p += csrc_bytes;

    /* Skip header extension if X bit set */
    __u8 x = (vpxcc >> 4) & 0x01;
    if (x) {
        /* Need extension header (4 bytes): profile(16) + length(16) */
        if ((void *)(p + 4) > data_end)
            return 0;

        /* length is number of 32-bit words following the 4-byte ext header */
        __u16 ext_len_words = ((__u16)p[2] << 8) | (__u16)p[3];
        __u32 ext_bytes = (__u32)ext_len_words * 4;

        /* Optional safety cap (keeps verifier & sanity happy) */
        if (ext_bytes > 2048)
            return 0;

        if ((void *)(p + 4 + ext_bytes) > data_end)
            return 0;

        p += 4 + ext_bytes;
    }

    *payload_out = p;


    /* Need at least 1 byte of payload for NAL parsing */
    if ((void *)(*payload_out + 1) > data_end)
        return 0;

    return 1;
}


static __always_inline int h264_get_nal_type(__u8 *payload, void *data_end, __u8 *nal_type_out)
{
    if ((void *)(payload + 1) > data_end)
        return 0;

    __u8 nal = payload[0];
    __u8 nal_type = nal & 0x1F;

    if (nal_type == 24) {
        if ((void *)(payload + 4) > data_end)
            return 0;
        /* first aggregated nal starts at payload+3 */
        __u8 inner = payload[3];
        *nal_type_out = inner & 0x1F;
        return 1;
    }

    /* FU-A (28): payload[1] has original nal type in low 5 bits */
    if (nal_type == 28) {
        if ((void *)(payload + 2) > data_end)
            return 0;
        __u8 fu_hdr = payload[1];
        nal_type = fu_hdr & 0x1F;
    }

    *nal_type_out = nal_type;
    return 1;
}

/* read 4 bytes safely */
static __always_inline int read_u32_be(__u8 *p, void *data_end, __u32 *out)
{
    if ((void *)(p + 4) > data_end)
        return 0;

    *out = ((__u32)p[0] << 24) |
           ((__u32)p[1] << 16) |
           ((__u32)p[2] <<  8) |
           ((__u32)p[3] <<  0);
    return 1;
}

static __always_inline __u32 get_bit32(__u32 w, __u32 pos /*0..31, MSB first*/)
{
    return (w >> (31 - pos)) & 1U;
}

/* decode one unsigned Exp-Golomb ue(v) from a 32-bit window */
static __always_inline int ue_decode32(__u32 w, __u32 *bitpos_io, __u32 *val_out)
{
    __u32 bitpos = *bitpos_io;
    __u32 zeros = 0;

    /* bounded scan for leading zeros (cap at 15) */
#pragma unroll
    for (int i = 0; i < 16; i++) {
        if (bitpos + zeros >= 32)
            return 0;
        if (get_bit32(w, bitpos + zeros) == 0)
            zeros++;
        else
            break;
    }

    if (bitpos + zeros >= 32)
        return 0;
    if (get_bit32(w, bitpos + zeros) != 1)
        return 0;

    /* consume zeros + the '1' */
    bitpos += zeros + 1;

    /* read info bits */
    if (zeros && bitpos + zeros > 32)
        return 0;

    __u32 info = 0;
#pragma unroll
    for (int j = 0; j < 16; j++) {
        if ((__u32)j < zeros) {
            info = (info << 1) | get_bit32(w, bitpos + j);
        }
    }
    bitpos += zeros;

    __u32 base = (zeros == 0) ? 0 : ((1U << zeros) - 1U);
    *val_out = base + info;
    *bitpos_io = bitpos;
    return 1;
}

static __always_inline int h264_get_slice_type(__u8 *payload, void *data_end, __u32 *slice_type_out)
{
    if ((void *)(payload + 2) > data_end)
        return 0;

    __u8 nal0 = payload[0];
    __u8 nal_type = nal0 & 0x1F;

    __u8 *rbsp = 0;

    if (nal_type == 28) {
        rbsp = payload + 2;     /* FU indicator + FU header are 2 bytes */
    } else { // nal-type 1
        rbsp = payload + 1;     /* skip 1-byte NAL header */
    }

    __u32 w = 0; // 4 byte window
    if (!read_u32_be(rbsp, data_end, &w))
        return 0;

    __u32 bitpos = 0;
    __u32 tmp = 0;

    /* first_mb_in_slice */
    if (!ue_decode32(w, &bitpos, &tmp))
        return 0;

    /* slice_type */
    __u32 slice_type = 0;
    if (!ue_decode32(w, &bitpos, &slice_type))
        return 0;

    *slice_type_out = slice_type;
    return 1;
}

static __always_inline void h264_process_single_nal(__u8 *payload, void *data_end, struct stream_state_shared *st)
{
    if ((void *)(payload + 1) > data_end)
        return;

    __u8 nal_type = payload[0] & 0x1F;

    /* IDR => I */
    if (nal_type == 5) {
        st->cur_type = 2;
        return;
    }

    /* non-IDR slice => parse slice_type */
    if (nal_type == 1) {
        __u32 slice_type = 0;
        if (h264_get_slice_type(payload, data_end, &slice_type)) {
            if (slice_type == 2 || slice_type == 7) {
                st->cur_type = 2; /* I-slice */
            } else if (slice_type == 0 || slice_type == 5) {
                if (st->cur_type != 2)
                    st->cur_type = 1; /* P-slice */
            }
        }
    }
}

static __always_inline void h264_process_fua(__u8 *payload, void *data_end, struct stream_state_shared *st)
{
    if ((void *)(payload + 2) > data_end)
        return;

    /* FU-A */
    __u8 fu_hdr = payload[1];
    __u8 start = (fu_hdr >> 7) & 0x1;
    __u8 orig_type = fu_hdr & 0x1F;

    if (orig_type != 1 && orig_type != 5)
        return;

    if (orig_type == 5) {
        st->cur_type = 2;
        return;
    }

    if (!start)
        return;


    /* orig_type == 1: parse slice_type from FU-A RBSP (payload+2) */
    __u32 slice_type = 0;
    if (h264_get_slice_type(payload, data_end, &slice_type)) {
        if (slice_type == 2 || slice_type == 7) {
            st->cur_type = 2;  /* I-slice */
        } else if (slice_type == 0 || slice_type == 5) {
            if (st->cur_type != 2)
                st->cur_type = 1; /* P-slice */
        }
    }
}

static __always_inline void h264_process_stap_a(__u8 *payload, void *data_end, struct stream_state_shared *st)
{
    __u8 *p = payload + 1;  /* skip STAP-A type byte */

#pragma unroll
    for (int i = 0; i < 8; i++) {
        /* bounds check re-establishes: p[0] and p[1] are readable */
        if ((void *)(p + 2) > data_end)
            return;

        __u32 size = ((__u32)p[0] << 8 | (__u32)p[1]) & 0xFFFF;
        p += 2;

        /* give verifier both min AND max bound on size */
        if (size == 0 || size > 1400)
            return;

        /* bounds check re-establishes: p[0..size-1] are readable */
        if ((void *)(p + size) > data_end)
            return;

        h264_process_single_nal(p, data_end, st);

        p += size;
    }
}

SEC("xdp")
int xdp_prog(struct xdp_md *ctx)
{

    void *data     = (void *)(long)ctx->data;
    void *data_end = (void *)(long)ctx->data_end;

    // Ethernet
    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end)
        return XDP_PASS;

    if (eth->h_proto != bpf_htons(ETH_P_IP))
        return XDP_PASS;

    // IPv4
    struct iphdr *ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end)
        return XDP_PASS;

    // UDP
    if (ip->protocol != 17)
        return XDP_PASS;

    // UDP (IP header length can vary)
    __u32 ihl = (__u32)ip->ihl * 4;
    if (ihl < sizeof(*ip))
        return XDP_PASS;
    if ((void *)((unsigned char *)ip + ihl) > data_end)
        return XDP_PASS;

    struct udphdr *udph = (void *)((unsigned char *)ip + ihl);
    if ((void *)(udph + 1) > data_end)
        return XDP_PASS;


    __u16 dport = bpf_ntohs(udph->dest);
    if (dport != 5004) {
        return XDP_PASS;
    }

    __u32 k = 0;

    struct network_stats *ns = bpf_map_lookup_elem(&network_stats, &k);
    if (!ns)
        return XDP_DROP;
    struct stream_stats *cs = bpf_map_lookup_elem(&stream_stats, &k);
    if (!cs)
        return XDP_DROP;
    struct stream_state_shared *st = bpf_map_lookup_elem(&stream_state, &k);
    if (!st)
        return XDP_DROP;
    struct packet_loss_stats *pl = bpf_map_lookup_elem(&packet_loss_stats, &k);
    if (!pl)
        return XDP_DROP;

    void *rtp = (void *)(udph + 1);

    __u8 marker = 0;
    __u32 ts = 0;
    __u16 seq     = 0;
    __u8 *payload = 0;
    __u32 ssrc = 0;

    if (!rtp_get_marker_ts_payload(rtp, data_end, &marker, &ts, &seq, &ssrc, &payload))
        return XDP_DROP;

    // Fake drop for packet loss testing
    // if (should_drop(seq))
    //     return XDP_DROP;

    ns->pkts++;
    ns->bytes += (__u64)((char *)data_end - (char *)data);
    pl->received_packets++;

    /* first seen packet */
    if (!st->seen_ts) {
        st->cur_ts = ts;
        st->cur_type = 0;
        st->seen_ts = 1;
        st->last_seq   = seq;
        st->ssrc     = ssrc;
        st->incomplete = 0;
        st->saw_marker = 0;
        pl->expected_packets++;

    }else if(ssrc != st->ssrc){ // new stream detected, reset everything

        st->ssrc     = ssrc;
        st->cur_ts   = ts;
        st->cur_type = 0;
        st->incomplete = 0;
        st->saw_marker = 0;
        st->last_seq = seq;
        pl->expected_packets++;

    } else if (st->cur_ts != ts) { 

        if (!st->saw_marker) { // marker packet lost
            cs->frames++;
            cs->incomplete_frames++;    // count frame as incomplete if we didn't see marker for it

            if (st->cur_type == 2)
                cs->incomplete_i_frames++;  
            else if (st->cur_type == 1)
                cs->incomplete_p_frames++; 
        }

        st->cur_ts = ts;
        st->cur_type = 0;
        st->incomplete = 0;
        st->saw_marker = 0;

         __u16 expected = st->last_seq + 1;
         if (seq != expected) {
            __u16 gap = seq - expected;
            pl->expected_packets += (__u64)gap + 1;
        } else {
            pl->expected_packets++;
        }
        st->last_seq = seq;

    }else{ // check for sequence gap
        __u16 expected = st->last_seq + 1;
        if (seq != expected) {
            __u16 gap = seq - expected;
            pl->expected_packets += (__u64)gap + 1;
            st->incomplete = 1;
        }
        else{
            pl->expected_packets++; 
        }
        st->last_seq = seq;
    }

    if ((void *)(payload + 1) <= data_end) {

        __u8 t = payload[0] & 0x1F;

        if (t == 1 || t == 5) {
            /* single NAL unit */
            h264_process_single_nal(payload, data_end, st);
        } else if (t == 28) {
            /* FU-A */
            h264_process_fua(payload, data_end, st);
        }
        else if (t == 24) {
            /* STAP-A */
            h264_process_stap_a(payload, data_end, st);
        }
    }

    if (marker) {
        st->saw_marker = 1;
        cs->frames++;

        if(st->incomplete){
            cs->incomplete_frames++;
            if (st->cur_type == 2)
                cs->incomplete_i_frames++;
            else if (st->cur_type == 1)
                cs->incomplete_p_frames++;  
        }
        else{
            if (st->cur_type == 2)
                cs->i_frames++;
            else if (st->cur_type == 1)
                cs->p_frames++;
        }

        st->cur_type = 0;
        st->incomplete = 0;
    }

    return XDP_PASS;
}

char LICENSE[] SEC("license") = "GPL";
