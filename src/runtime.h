
#define LOG(...) fprintf(stderr, __VA_ARGS__)

#ifndef RELEASE
#define DEBUG(...) fprintf(stderr, __VA_ARGS__)
#else
#define DEBUG(...)
#endif

#define FATAL(COND, MSG) { if(COND) { LOG(MSG); exit(-1); } }

#define MAX_REQUEST_SIZE 6 * 1048576
#define MAX_RESPONSE_SIZE 6 * 1048576
#define MAX_HTTP_HEADER_SIZE 8 * 1024
#define INCOMING_LAMBDA_REQUEST_BUFFER_SIZE (MAX_REQUEST_SIZE + MAX_HTTP_HEADER_SIZE)
#define OUTGOING_LAMBDA_RESPONSE_BUFFER_SIZE (MAX_RESPONSE_SIZE)

typedef struct {
    char *data;
    size_t len;
} slice;

typedef struct {
    slice buffer;
    slice awsRequestId;
    slice body;
} http_recv_buffer;

// higher-level interface ("bring your own handler"):

void start_lambda(int (*handler)(const http_recv_buffer*, char *));

// lower-level interface ("bring your own loop") - simplifies access via Rust FFI:

typedef struct runtime runtime;

runtime* runtime_init();

char *get_response_buffer(const runtime *rt);

http_recv_buffer* get_next_request(const runtime *rt);

void send_response(const runtime *rt, const char *response_buffer, size_t response_len);
