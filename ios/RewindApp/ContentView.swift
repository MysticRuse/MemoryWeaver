import SwiftUI
import PhotosUI

struct ContentView: View {
    // Session variables
    @State private var sessionId: String = "family-trip-california-road-trip-3f3eaf"
    @State private var contributorName: String = "Grandma Lena"
    @State private var journalType: String = "trip"
    
    // UI state
    @State private var isUploading = false
    @State private var isCurationRunning = false
    @State private var curationProgress: Double = 0.0
    @State private var curationStatus: String = "Ready"
    
    // Curated items mock data (reflecting the 3-step Grandma flow)
    @State private var selectedItems: [PhotoItem] = []
    @State private var publishedLink: String? = nil
    
    let journalTypes = [
        "trip": "🏞️ Family Trip Journal",
        "reunion": "💖 Family Precious Moments",
        "other": "📅 Daily Personal Diary",
        "friends": "🏫 School Co-Journal"
    ]
    
    var body: some View {
        NavigationView {
            ScrollView {
                VStack(spacing: 24) {
                    
                    // --- STEP 1: CHOOSE JOURNAL CONTEXT & IDENTITY ---
                    VStack(alignment: .leading, spacing: 14) {
                        Text("Step 1: Setup Your Journal")
                            .font(.headline)
                            .foregroundColor(.secondary)
                        
                        VStack(spacing: 12) {
                            HStack {
                                Text("✍️ Who's writing?")
                                Spacer()
                                TextField("e.g. Grandma Lena", text: $contributorName)
                                    .textFieldStyle(RoundedBorderTextFieldStyle())
                                    .frame(width: 180)
                                    .multilineTextAlignment(.trailing)
                            }
                            
                            HStack {
                                Text("🏷️ Scope Theme")
                                Spacer()
                                Picker("Theme", selection: $journalType) {
                                    ForEach(journalTypes.keys.sorted(), id: \.self) { key in
                                        Text(self.journalTypes[key] ?? "").tag(key)
                                    }
                                }
                                .pickerStyle(MenuPickerStyle())
                            }
                        }
                        .padding()
                        .background(Color(.secondarySystemBackground))
                        .cornerRadius(12)
                    }
                    .padding(.horizontal)
                    
                    // --- STEP 2: POINT THE PHOTOS (Select / Upload) ---
                    VStack(alignment: .leading, spacing: 14) {
                        Text("Step 2: Add Your Photos")
                            .font(.headline)
                            .foregroundColor(.secondary)
                        
                        Button(action: selectPhotos) {
                            HStack {
                                Image(systemName: "photo.on.rectangle.angled")
                                    .font(.title2)
                                Text("📸 Choose Photos from Camera Roll")
                                    .fontWeight(.bold)
                            }
                            .frame(maxWidth: .infinity)
                            .padding()
                            .background(Color.blue)
                            .foregroundColor(.white)
                            .cornerRadius(12)
                        }
                        
                        if isUploading {
                            HStack {
                                ProgressView()
                                    .padding(.trailing, 8)
                                Text("Sending high-res images to safe storage...")
                                    .font(.subheadline)
                                    .foregroundColor(.secondary)
                            }
                            .padding()
                            .frame(maxWidth: .infinity)
                            .background(Color(.secondarySystemBackground))
                            .cornerRadius(12)
                        } else if !selectedItems.isEmpty {
                            Text("Selected \(selectedItems.count) photos for the album:")
                                .font(.subheadline)
                                .foregroundColor(.secondary)
                            
                            ScrollView(.horizontal, showsIndicators: false) {
                                HStack(spacing: 10) {
                                    ForEach(selectedItems) { item in
                                        ZStack(alignment: .topTrailing) {
                                            Image(systemName: "photo")
                                                .resizable()
                                                .aspectRatio(contentMode: .fill)
                                                .frame(width: 80, height: 80)
                                                .background(Color.gray.opacity(0.2))
                                                .cornerRadius(8)
                                            
                                            Button(action: { removePhoto(item) }) {
                                                Image(systemName: "xmark.circle.fill")
                                                    .foregroundColor(.red)
                                                    .background(Color.white.clipShape(Circle()))
                                            }
                                            .offset(x: 5, y: -5)
                                        }
                                    }
                                }
                                .padding(.vertical, 8)
                            }
                        }
                    }
                    .padding(.horizontal)
                    
                    // --- STEP 3: TRUST, PRICING & AUTO-CURATION DISCLOSURES ---
                    VStack(spacing: 16) {
                        // Trust & Security
                        HStack(alignment: .top) {
                            Image(systemName: "shield.lefthalf.filled")
                                .foregroundColor(.blue)
                                .font(.title3)
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Privacy & Security Shield Active")
                                    .font(.subheadline)
                                    .fontWeight(.bold)
                                Text("GPS/camera tags are automatically stripped from links. Your original files are stored in isolated Google Cloud vaults.")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                        }
                        
                        Divider()
                        
                        // Upfront pricing
                        HStack(alignment: .top) {
                            Image(systemName: "creditcard")
                                .foregroundColor(.blue)
                                .font(.title3)
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Simple Upfront Flat Terms")
                                    .font(.subheadline)
                                    .fontWeight(.bold)
                                Text("Cloud Sharing & Sync: $49.99/year flat.\nHardcover Linen Keepsake: $39.00 flat per copy.")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                        }
                    }
                    .padding()
                    .background(Color(.secondarySystemBackground))
                    .cornerRadius(12)
                    .padding(.horizontal)
                    
                    // --- STEP 4: AUTO-CURATE & PUBLISH BUTTON ---
                    VStack(spacing: 16) {
                        if isCurationRunning {
                            VStack(spacing: 8) {
                                ProgressView(value: curationProgress, total: 1.0)
                                    .progressViewStyle(LinearProgressViewStyle(tint: .blue))
                                Text(curationStatus)
                                    .font(.subheadline)
                                    .foregroundColor(.secondary)
                            }
                            .padding()
                            .background(Color(.secondarySystemBackground))
                            .cornerRadius(12)
                        } else {
                            Button(action: runCurationPipeline) {
                                HStack {
                                    Image(systemName: "sparkles")
                                    Text("✨ Publish Memory Book")
                                        .fontWeight(.bold)
                                }
                                .frame(maxWidth: .infinity)
                                .padding()
                                .background(selectedItems.isEmpty ? Color.gray : Color.blue)
                                .foregroundColor(.white)
                                .cornerRadius(12)
                            }
                            .disabled(selectedItems.isEmpty)
                        }
                        
                        if let link = publishedLink {
                            VStack(spacing: 10) {
                                Text("🎉 Your Keepsake is Ready!")
                                    .font(.headline)
                                    .foregroundColor(.green)
                                
                                Link(destination: URL(string: link)!) {
                                    HStack {
                                        Image(systemName: "book.closed")
                                        Text("Open Curation Viewer")
                                    }
                                    .foregroundColor(.blue)
                                }
                            }
                            .padding()
                            .frame(maxWidth: .infinity)
                            .background(Color.green.opacity(0.1))
                            .cornerRadius(12)
                        }
                    }
                    .padding(.horizontal)
                    
                }
                .padding(.vertical)
            }
            .navigationTitle("Rewind Journal")
        }
    }
    
    // Action functions
    func selectPhotos() {
        isUploading = true
        // Mocking user selection of photos
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            self.selectedItems = (1...12).map { PhotoItem(id: UUID(), name: "photo_\($0).jpg") }
            self.isUploading = false
        }
    }
    
    func removePhoto(_ item: PhotoItem) {
        selectedItems.removeAll { $0.id == item.id }
    }
    
    func runCurationPipeline() {
        isCurationRunning = true
        curationStatus = "Phase 1/3: Blurry & Screenshot moderation..."
        curationProgress = 0.3
        
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
            self.curationStatus = "Phase 2/3: Semantic grouping & Curation grading..."
            self.curationProgress = 0.6
            
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                self.curationStatus = "Phase 3/3: Narrating your trip journal..."
                self.curationProgress = 0.9
                
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
                    self.curationProgress = 1.0
                    self.isCurationRunning = false
                    self.publishedLink = "http://localhost:8000/viewer?session=\(self.sessionId)"
                }
            }
        }
    }
}

struct PhotoItem: Identifiable {
    let id: UUID
    let name: String
}

struct ContentView_Previews: PreviewProvider {
    static var previews: some View {
        ContentView()
    }
}
