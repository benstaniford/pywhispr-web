#!/bin/bash

# pywhispr-web - Build and Run Script

echo "🚀 pywhispr-web docker setup"
echo "================================"

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker is not running. Please start Docker and try again."
    exit 1
fi

# Build and run with Docker Compose
echo "🔨 Building and starting pywhispr-web..."
docker-compose up --build -d

if [ $? -eq 0 ]; then
    echo ""
    echo "🎉 pywhispr-web is now running!"
    echo "📱 Access the application at: http://localhost:5000"
    echo ""
    echo "🛑 To stop the application, run: docker-compose down"
    echo "📊 To view logs, run: docker-compose logs -f"
else
    echo "❌ Failed to start pywhispr-web. Check the logs for errors."
    docker-compose logs
fi
