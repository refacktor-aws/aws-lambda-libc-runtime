#ifndef __APPLE__
#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <netdb.h>
#include <netinet/ip.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <sys/mman.h>
#include <errno.h>
#include <sys/time.h> 

#include "runtime.h"

struct runtime {
    http_recv_buffer *hb;
    struct addrinfo *runtime_addrinfo;
    const char *runtime_api;
    char *response_buffer;
};

static inline struct addrinfo *resolve_host(const char *endpoint)
{
    DEBUG("Resolving endpoint: %s\n", endpoint);

    char *colon = strchr(endpoint, ':');
    char host_no_port[strlen(endpoint) + 1];
    char *port;
    if (colon != NULL)
    {
        strncpy(host_no_port, endpoint, colon - endpoint);
        host_no_port[colon - endpoint] = '\0';
        port = colon + 1;
    }
    else
    {
        strcpy(host_no_port, endpoint);
        port = "80";
    }
    DEBUG("Parsed endpoint into host=[%s] and port=[%s]\n", host_no_port, port);

    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_protocol = 0;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_family = AF_INET;
    hints.ai_flags = AI_NUMERICSERV | AI_ADDRCONFIG;
    struct addrinfo *dns_result;

    int rc = getaddrinfo(host_no_port, port, &hints, &dns_result);
    DEBUG("getaddrinfo[%s:%s] returned rc=%d\n", host_no_port, port, rc);
    FATAL(rc != 0, "getaddrinfo failed");
    (void)rc;
    return dns_result;
}

static int send_all(int sockfd, const char *buf, int len)
{
    int total_sent = 0;
    while (total_sent < len)
    {
        int rc = send(sockfd, buf + total_sent, len - total_sent, 0);
        FATAL(rc < 0, "Failed to send data\n");
        total_sent += rc;
        DEBUG("Sent %d bytes, total sent=%d\n", rc, total_sent);
    }
    return total_sent;
}

static inline int socket_connect(const struct addrinfo *addr)
{
    DEBUG("Creating socket\n");
    int sockfd = socket(AF_INET, SOCK_STREAM, 0);
    FATAL(sockfd < 0, "Failed to create socket\n");

    DEBUG("Setting socket timeout\n");
    struct timeval timeout = {
        .tv_sec = 1,
        .tv_usec = 0
    };
    
    int rc = setsockopt(sockfd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    if (rc < 0) {
        perror("setsockopt failed");
        FATAL(rc < 0, "Failed to set socket timeout\n");
    }

    DEBUG("Connecting socket\n");
    rc = connect(sockfd, addr->ai_addr, addr->ai_addrlen);

    FATAL(rc < 0, "Connection failed\n");
    DEBUG("Connected\n");
    return sockfd;
}

static void http(const runtime *rt, const char *path, const char *method, const char *content, int req_content_length)
{
    const char *host = rt->runtime_api;
    const struct addrinfo *addr = rt->runtime_addrinfo;
    http_recv_buffer *hb = rt->hb;

    DEBUG("Making HTTP request to host=[%s], path=[%s], method=[%s]\n", host, path, method);
    int sockfd = socket_connect(addr);

    char request[MAX_HTTP_HEADER_SIZE];
    snprintf(request, sizeof(request),
             "%s %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"
             "Content-Length: %d\r\n\r\n",
             method, path, host, req_content_length);

    DEBUG("Request headers:\n<<<\n%s\n>>>\n", request);
    send_all(sockfd, request, strlen(request));

    if (req_content_length > 0)
    {
        send_all(sockfd, content, req_content_length);
    }

    char *response = hb->buffer.data;
    char *parse_point = response;
    char *line_start = response;
    char *body_start = NULL;
    int total_bytes_received = 0;
    int content_length = -1;
    int remain = INCOMING_LAMBDA_REQUEST_BUFFER_SIZE;
    char *delimiter = NULL;
    char *chunk_start = NULL;
    char *chunk_end = NULL;
    char *chunk_move = NULL;
    hb->body.data = NULL;
    hb->awsRequestId.data = NULL;

    enum parser_state {state_header, state_body, state_chunk};
    enum parser_state state = state_header;

    while (remain)
    {

        FATAL(parse_point >= response + INCOMING_LAMBDA_REQUEST_BUFFER_SIZE, "Buffer overflow");

        if (parse_point >= response + total_bytes_received)
        {
            DEBUG("Calling recv with ptr=%d, remain=%d\n", total_bytes_received, remain);
            int bytes_received;
            do
            {
                bytes_received = recv(sockfd, response + total_bytes_received, remain, 0);
            } while (bytes_received < 0 && errno == EINTR);

            DEBUG("Received %d bytes\n", bytes_received);
            FATAL(bytes_received <= 0, "Failed to receive bytes\n");

            total_bytes_received += bytes_received;
            remain -= bytes_received;
            if (body_start != NULL && state == state_body)
            {
                parse_point += bytes_received;
            }
        }

        if (state == state_header)
        {
            if (*parse_point == ':')
            {
                delimiter = parse_point;
            }
            else if (*parse_point == '\n')
            {
                FATAL(parse_point - response < 3, "Unexpected linebreak before HTTP headers");
                FATAL(parse_point[-1] != '\r', "Malformed linebreak: no \\r before \\n");
                if (parse_point[-2] == '\n')
                {
                    if (!chunk_end) {
                        FATAL(content_length < 0, "Missing Content-Length header.");
                        body_start = parse_point + 1;
                        remain = content_length - ((response + total_bytes_received) - body_start);
                        DEBUG("BODY START: %p, remain=%d\n", body_start, remain);
                        parse_point = response + total_bytes_received;
                        state = state_body;
                        continue;
                    } else {
                        body_start = parse_point + 1;
                        chunk_end = body_start;
                        chunk_move = body_start;
                        state = state_chunk;
                    }
                }
                if (delimiter == NULL)
                {
                    if (!memcmp(line_start, "HTTP/1.0 410", 12))
                    {
                        DEBUG("410 (shutting down)\n"); // for testing only
                        exit(0);
                    }
                }
                else if (!strncmp(line_start, "Content-Length:", delimiter - line_start))
                {
                    content_length = atoi(delimiter + 2);
                    DEBUG("HEADER Content-Length: %d\n", content_length);
                }
                else if (!strncmp(line_start, "Lambda-Runtime-Aws-Request-Id:", delimiter - line_start))
                {
                    hb->awsRequestId.data = delimiter + 2;
                    hb->awsRequestId.len = (parse_point - hb->awsRequestId.data) - 1;
                    DEBUG("HEADER Lambda-Runtime-Aws-Request-Id: [%.*s]\n", (int) hb->awsRequestId.len, hb->awsRequestId.data);
                }
                else if (!strncmp(line_start, "Transfer-Encoding: chunked", parse_point - line_start))
                {
                    chunk_end = parse_point;
                    DEBUG("HEADER Transfer-Encoding: chunked\n");
                }
                else if (!strncmp(line_start, "Transfer-Encoding: chunked", parse_point - 1 - line_start))
                {
                    chunk_end = parse_point;
                    DEBUG("HEADER Transfer-Encoding: chunked\n");
                }
                line_start = parse_point + 1;
                delimiter = NULL;
            }

            ++parse_point;
        }
        else if (state == state_chunk && parse_point < response + total_bytes_received)
        {
            if (*parse_point == '\n')
            {
                if (chunk_start && chunk_end) {
                    memmove(chunk_move, chunk_start, chunk_end - chunk_start);
                    chunk_move += chunk_end - chunk_start;
                }

                chunk_start = parse_point + 1;
                long chunk_size = strtol(chunk_end, NULL, 16);
                DEBUG("Chunk size: %ld\n", chunk_size);

                if (!chunk_size)
                {
                    break;
                }

                chunk_end = chunk_start + chunk_size;
                parse_point = chunk_end + 2; // 2 for terminating crlf
            } else {
                ++parse_point;
            }
        }
    }
    DEBUG("Total bytes received: %d\n", total_bytes_received);
    hb->body.data = body_start;
    if (state == state_body) {
        response[total_bytes_received] = '\0';
        hb->buffer.len = total_bytes_received;
        hb->body.len = total_bytes_received - (body_start - response);
    } else if (state == state_chunk) {
        *chunk_move = '\0';
        hb->buffer.len = chunk_move - response;
        hb->body.len = chunk_move - body_start;
    }
    close(sockfd);
    DEBUG("Response received\n");
}

static void *mapalloc(size_t size)
{
    int pagesize = getpagesize();
    size = (size | (pagesize - 1)) + 1 + pagesize;

    void *ptr = mmap(NULL, size, 
        PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE,
        -1, 0);

    FATAL(ptr == MAP_FAILED, "Failed to allocate memory\n");
    // set a SEGV trap at the end of the buffer
    mprotect(ptr + (size - pagesize), pagesize, PROT_NONE);
    DEBUG("Allocated %d bytes at %p\n", size, ptr);
    return ptr;
}

runtime* runtime_init() {
    // using static here is fine because we only have to process one request at a time.
    static runtime rt;
    static http_recv_buffer hb;
    rt.hb = &hb;

    rt.runtime_api = getenv("AWS_LAMBDA_RUNTIME_API");
    FATAL(rt.runtime_api == NULL, "AWS_LAMBDA_RUNTIME_API environment variable not set\n");
    DEBUG("Runtime API: %s\n", rt.runtime_api);
    rt.runtime_addrinfo = resolve_host(rt.runtime_api);

    hb.buffer.data = mapalloc(INCOMING_LAMBDA_REQUEST_BUFFER_SIZE);
    rt.response_buffer = mapalloc(OUTGOING_LAMBDA_RESPONSE_BUFFER_SIZE);
    return &rt;
}

http_recv_buffer* get_next_request(const runtime *rt) {
    char *path =  "/2018-06-01/runtime/invocation/next";
    DEBUG("Getting next request\n");
    http(rt, path, "GET", "", 0);
    FATAL(rt->hb->awsRequestId.data == NULL, "Missing Lambda-Runtime-Aws-Request-Id header\n");
    return rt->hb;
}

char *get_response_buffer(const runtime *rt) {
    return rt->response_buffer;
}

void send_response(const runtime *rt, const char *response, size_t response_len) {
    char path[256];
    snprintf(path, sizeof(path), 
             "/2018-06-01/runtime/invocation/%.*s/response", 
             (int) rt->hb->awsRequestId.len, rt->hb->awsRequestId.data);
    http(rt, path, "POST", response, response_len);
}

void start_lambda(int (*handler)(const http_recv_buffer *, char *))
{
    runtime *rt = runtime_init();
    char *output_buffer = get_response_buffer(rt);

    while (1) {
        http_recv_buffer *hb = get_next_request(rt);
        int lambda_response_length = handler(hb, output_buffer);
        send_response(rt, output_buffer, lambda_response_length);
    }
}
