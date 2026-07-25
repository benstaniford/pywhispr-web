#!/bin/bash

# Docker Container Test Script
# Tests the pywhispr-web container build and functionality

set -e  # Exit on any error

echo "🐳 pywhispr-web Container Test Suite"
echo "========================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Test configuration
CONTAINER_NAME="pywhispr-web-test"
TEST_PORT="5001"
TIMEOUT=30

# Determine which compose file to use
if [ -n "$COMPOSE_FILE" ]; then
    COMPOSE_CMD="docker compose -f $COMPOSE_FILE"
    echo -e "${BLUE}📋 Using compose file: $COMPOSE_FILE${NC}"
elif [ -f "docker-compose.test.yml" ]; then
    COMPOSE_CMD="docker compose -f docker-compose.test.yml"
    echo -e "${BLUE}📋 Using test compose file: docker-compose.test.yml${NC}"
else
    COMPOSE_CMD="docker compose"
    echo -e "${BLUE}📋 Using default compose file: docker-compose.yml${NC}"
fi

# Cleanup function
cleanup() {
    echo -e "${YELLOW}🧹 Cleaning up test environment...${NC}"
    
    # Ensure we're in the project root directory
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
    cd "$PROJECT_ROOT" 2>/dev/null || true
    
    docker compose down --remove-orphans 2>/dev/null || true
    $COMPOSE_CMD down --remove-orphans 2>/dev/null || true
    docker rm -f $CONTAINER_NAME 2>/dev/null || true
    rm -f test-response.html api-response.json app-response.html 2>/dev/null || true
    echo -e "${GREEN}✅ Cleanup completed${NC}"
}

# Set cleanup trap
trap cleanup EXIT

# Function to wait for container to be healthy
wait_for_healthy() {
    local max_attempts=30
    local attempt=1
    
    echo -e "${BLUE}⏳ Waiting for container to be healthy...${NC}"
    while [ $attempt -le $max_attempts ]; do
        if docker ps --format "table {{.Names}}\t{{.Status}}" | grep -q "healthy"; then
            echo -e "${GREEN}✅ Container is healthy${NC}"
            return 0
        fi
        echo -e "${YELLOW}   Attempt $attempt/$max_attempts - waiting...${NC}"
        sleep 2
        ((attempt++))
    done
    
    echo -e "${RED}❌ Container failed to become healthy within $((max_attempts * 2)) seconds${NC}"
    return 1
}

# Test 1: Build the container
echo -e "${BLUE}📦 Test 1: Building Docker container...${NC}"

# Ensure we're in the project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Verify docker-compose.yml exists (skip check in CI with COMPOSE_FILE set)
if [ -z "$COMPOSE_FILE" ] && [ ! -f "docker-compose.yml" ]; then
    echo -e "${RED}❌ docker-compose.yml not found in $PWD${NC}"
    echo "Expected to find it in: $PROJECT_ROOT"
    exit 1
fi

$COMPOSE_CMD build --no-cache
if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ Docker build successful${NC}"
else
    echo -e "${RED}❌ Docker build failed${NC}"
    exit 1
fi
echo ""

# Test 2: Start the container
echo -e "${BLUE}🚀 Test 2: Starting container...${NC}"
$COMPOSE_CMD up -d
if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ Container started${NC}"
else
    echo -e "${RED}❌ Container failed to start${NC}"
    exit 1
fi

# Wait for container to be healthy
wait_for_healthy

echo ""

# Test 3: Health check
echo -e "${BLUE}🩺 Test 3: Testing health endpoint...${NC}"
health_response=$(curl -s http://localhost:5000/health)
if echo "$health_response" | grep -q '"status":"healthy"'; then
    echo -e "${GREEN}✅ Health check passed${NC}"
    echo "   Response: $health_response"
else
    echo -e "${RED}❌ Health check failed${NC}"
    echo "   Response: $health_response"
    exit 1
fi
echo ""

# Test 4: Server configuration API
echo -e "${BLUE}🔗 Test 6: Testing server configuration API...${NC}"
api_response=$(curl -s -w "%{http_code}" \
    http://localhost:5000/api/servers \
    -o api-response.json)

if [ "$api_response" = "200" ]; then
    echo -e "${GREEN}✅ Server configuration API working (HTTP $api_response)${NC}"
    # Check if response contains expected data
    if grep -q '"servers"' api-response.json && grep -q '"cache_ttl_seconds"' api-response.json; then
        echo -e "${GREEN}   Response contains expected JSON structure${NC}"
    else
        echo -e "${YELLOW}   Warning: Response may not contain expected data structure${NC}"
    fi
    rm -f api-response.json
else
    echo -e "${RED}❌ Server configuration API failed (HTTP $api_response)${NC}"
    exit 1
fi
echo ""

# Test 4b: The config volume must be writable by the non-root container user,
# otherwise saving servers fails only once a real user tries it.
echo -e "${BLUE}💾 Test 4b: Testing server configuration is writable...${NC}"
put_response=$(curl -s -w "%{http_code}" \
    -X PUT \
    -H "Content-Type: application/json" \
    -d '{"servers":[{"name":"container-test","url":"127.0.0.1:9149"}],"cache_ttl_seconds":90}' \
    http://localhost:5000/api/servers \
    -o put-response.json)

if [ "$put_response" = "200" ] && grep -q 'http://127.0.0.1:9149' put-response.json; then
    echo -e "${GREEN}✅ Server list saved and normalised (HTTP $put_response)${NC}"

    # Reading it back proves it reached the volume, not just one worker's memory.
    curl -s http://localhost:5000/api/servers -o reread-response.json
    if grep -q 'container-test' reread-response.json; then
        echo -e "${GREEN}   Configuration persisted to the data volume${NC}"
    else
        echo -e "${RED}❌ Configuration did not persist${NC}"
        rm -f put-response.json reread-response.json
        exit 1
    fi

    # Leave the container as we found it.
    curl -s -X PUT -H "Content-Type: application/json" \
        -d '{"servers":[],"cache_ttl_seconds":60}' \
        http://localhost:5000/api/servers -o /dev/null
    rm -f put-response.json reread-response.json
else
    echo -e "${RED}❌ Could not save server configuration (HTTP $put_response)${NC}"
    cat put-response.json 2>/dev/null
    rm -f put-response.json
    exit 1
fi
echo ""

# Test 4c: With no servers configured, the app must say so cleanly rather than
# erroring, since that is exactly the state a new install is in.
echo -e "${BLUE}🎙️  Test 4c: Testing readiness with no servers configured...${NC}"
ready_response=$(curl -s -w "%{http_code}" \
    http://localhost:5000/api/ready \
    -o ready-response.json)

if [ "$ready_response" = "200" ] && grep -q '"ready": *false' ready-response.json; then
    echo -e "${GREEN}✅ Readiness reports no server available (HTTP $ready_response)${NC}"
    rm -f ready-response.json
else
    echo -e "${RED}❌ Readiness endpoint unexpected response (HTTP $ready_response)${NC}"
    cat ready-response.json 2>/dev/null
    rm -f ready-response.json
    exit 1
fi
echo ""

# Test 4d: The front-end is useless if its assets are not in the image; the
# Dockerfile copies static/ explicitly and this catches a missed COPY.
echo -e "${BLUE}📦 Test 4d: Testing static assets are served...${NC}"
static_failed=0
for asset in css/app.css js/app.js js/recorder.js js/capture-worklet.js js/settings.js manifest.webmanifest icons/icon-512.png; do
    asset_code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:5000/static/$asset")
    if [ "$asset_code" = "200" ]; then
        echo -e "${GREEN}   ✅ $asset${NC}"
    else
        echo -e "${RED}   ❌ $asset (HTTP $asset_code)${NC}"
        static_failed=1
    fi
done

if [ "$static_failed" = "1" ]; then
    echo -e "${RED}❌ Static assets missing from the image${NC}"
    exit 1
fi
echo -e "${GREEN}✅ All static assets served${NC}"
echo ""

# Test 5: Main editor page
echo -e "${BLUE}📝 Test 5: Testing main editor page...${NC}"
app_response=$(curl -s -w "%{http_code}" \
    http://localhost:5000/ \
    -o app-response.html)

if [ "$app_response" = "200" ]; then
    echo -e "${GREEN}✅ Main application page accessible (HTTP $app_response)${NC}"

    # Check if page contains expected content
    if grep -q "PyWhispr Web" app-response.html; then
        echo -e "${GREEN}   Page contains expected application content${NC}"
    else
        echo -e "${YELLOW}   Warning: Page may not contain expected content${NC}"
    fi

    # The editor and record button are the app; a rendered shell without them
    # would mean the template stopped extending base.html correctly.
    if grep -q 'id="editor"' app-response.html && grep -q 'id="record"' app-response.html; then
        echo -e "${GREEN}   Editor and record button present${NC}"
    else
        echo -e "${RED}❌ Editor markup missing from the page${NC}"
        rm -f app-response.html
        exit 1
    fi

    rm -f app-response.html
else
    echo -e "${RED}❌ Main application page failed (HTTP $app_response)${NC}"
    exit 1
fi
echo ""

# Test 6: Container logs check
echo -e "${BLUE}📋 Test 6: Checking container logs for errors...${NC}"
error_count=$($COMPOSE_CMD logs pywhispr-web 2>&1 | grep -i -c "error\|exception\|traceback" || true)
if [ "$error_count" -eq 0 ]; then
    echo -e "${GREEN}✅ No errors found in container logs${NC}"
else
    echo -e "${YELLOW}⚠️  Found $error_count potential error(s) in logs${NC}"
    echo "Recent logs:"
    $COMPOSE_CMD logs --tail=10 pywhispr-web
fi
echo ""

# Test 7: Performance test
echo -e "${BLUE}⚡ Test 7: Basic performance test...${NC}"
start_time=$(date +%s%N)
perf_response=$(curl -s -w "%{http_code}" \
    http://localhost:5000/api/servers \
    -o /dev/null)
end_time=$(date +%s%N)

response_time=$(( (end_time - start_time) / 1000000 )) # Convert to milliseconds

if [ "$perf_response" = "200" ]; then
    echo -e "${GREEN}✅ Performance test passed (${response_time}ms)${NC}"
    if [ "$response_time" -lt 1000 ]; then
        echo -e "${GREEN}   Excellent response time${NC}"
    elif [ "$response_time" -lt 3000 ]; then
        echo -e "${YELLOW}   Good response time${NC}"
    else
        echo -e "${YELLOW}   Response time could be improved${NC}"
    fi
else
    echo -e "${RED}❌ Performance test failed (HTTP $perf_response)${NC}"
    exit 1
fi
echo ""

# Final summary
echo -e "${GREEN}🎉 All tests completed successfully!${NC}"
echo ""
echo -e "${BLUE}📊 Test Summary:${NC}"
echo -e "${GREEN}✅ Docker build${NC}"
echo -e "${GREEN}✅ Container startup${NC}"
echo -e "${GREEN}✅ Health check${NC}"
echo -e "${GREEN}✅ Server configuration API${NC}"
echo -e "${GREEN}✅ Configuration persistence${NC}"
echo -e "${GREEN}✅ Static assets${NC}"
echo -e "${GREEN}✅ Main editor page${NC}"
echo -e "${GREEN}✅ Container logs${NC}"
echo -e "${GREEN}✅ Performance test${NC}"
echo ""
echo -e "${BLUE}🌐 Application is ready at: http://localhost:5000${NC}"
echo ""
echo -e "${GREEN}✨ Test suite completed successfully! ✨${NC}"