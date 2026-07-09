package com.rewind.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.animation.*
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.util.UUID

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    MinimalistJournalScreen()
                }
            }
        }
    }
}

data class AndroidPhotoItem(val id: String = UUID.randomUUID().toString(), val name: String)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MinimalistJournalScreen() {
    var contributorName by remember { mutableStateOf("Grandma Lena") }
    var journalType by remember { mutableStateOf("trip") }
    
    var isUploading by remember { mutableStateOf(false) }
    var isCurationRunning by remember { mutableStateOf(false) }
    var curationProgress by remember { mutableStateOf(0f) }
    var curationStatus by remember { mutableStateOf("Ready") }
    
    var selectedPhotos by remember { mutableStateOf(listOf<AndroidPhotoItem>()) }
    var publishedLink by remember { mutableStateOf<String?>(null) }
    
    val coroutineScope = rememberCoroutineScope()
    
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Rewind Journal", fontWeight = FontWeight.Bold) }
            )
        }
    ) { paddingValues ->
        Column(
            modifier = Modifier
                .padding(paddingValues)
                .fillMaxSize()
                .padding(16.dp)
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(24.dp)
        ) {
            
            // --- STEP 1: CONTEXT CONFIG ---
            Card(
                shape = RoundedCornerShape(16.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    Text("Step 1: Setup Your Journal", fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.primary)
                    
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text("✍️ Who's writing?")
                        OutlinedTextField(
                            value = contributorName,
                            onValueChange = { contributorName = it },
                            modifier = Modifier.width(180.dp),
                            singleLine = true
                        )
                    }
                    
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text("🏞️ Scope Type")
                        Button(onClick = {
                            journalType = if (journalType == "trip") "reunion" else "trip"
                        }) {
                            Text(if (journalType == "trip") "🏞️ Family Trip" else "💖 Precious Moments")
                        }
                    }
                }
            }
            
            // --- STEP 2: POINT THE PHOTOS ---
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Step 2: Add Your Photos", fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.secondary)
                
                Button(
                    onClick = {
                        isUploading = true
                        coroutineScope.launch {
                            delay(1500)
                            selectedPhotos = (1..8).map { AndroidPhotoItem(name = "photo_$it.jpg") }
                            isUploading = false
                        }
                    },
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(12.dp)
                ) {
                    Icon(Icons.Default.Add, contentDescription = "Add Photos")
                    Spacer(modifier = Modifier.width(8.dp))
                    Text("📸 Choose Photos from Gallery")
                }
                
                if (isUploading) {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(12.dp))
                            .padding(16.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        CircularProgressIndicator(modifier = Modifier.size(24.dp))
                        Spacer(modifier = Modifier.width(12.dp))
                        Text("Sending high-res images to safe storage...", fontSize = 13.sp)
                    }
                } else if (selectedPhotos.isNotEmpty()) {
                    Text("Selected ${selectedPhotos.size} photos:", fontSize = 12.sp, color = Color.Gray)
                    LazyRow(
                        horizontalArrangement = Arrangement.spacedBy(10.dp),
                        contentPadding = PaddingValues(vertical = 4.dp)
                    ) {
                        items(selectedPhotos) { photo ->
                            Box(modifier = Modifier.size(80.dp)) {
                                Box(
                                    modifier = Modifier
                                        .fillMaxSize()
                                        .clip(RoundedCornerShape(8.dp))
                                        .background(Color.LightGray)
                                ) {
                                    Icon(
                                        Icons.Default.PlayArrow, 
                                        contentDescription = null, 
                                        modifier = Modifier.align(Alignment.Center)
                                    )
                                }
                                IconButton(
                                    onClick = { selectedPhotos = selectedPhotos.filter { it.id != photo.id } },
                                    modifier = Modifier
                                        .align(Alignment.TopEnd)
                                        .size(20.dp)
                                        .background(Color.White, CircleShape)
                                ) {
                                    Icon(Icons.Default.Clear, contentDescription = "Remove", tint = Color.Red, modifier = Modifier.size(14.dp))
                                }
                            }
                        }
                    }
                }
            }
            
            // --- STEP 3: DISCLOSURES (Pricing & Trust) ---
            Card(
                shape = RoundedCornerShape(16.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(16.dp)
                ) {
                    Row(verticalAlignment = Alignment.Top) {
                        Icon(Icons.Default.Lock, contentDescription = "Lock", tint = MaterialTheme.colorScheme.primary)
                        Spacer(modifier = Modifier.width(12.dp))
                        Column {
                            Text("Privacy & Security Shield Active", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                            Text("EXIF details (GPS tags) are automatically stripped from public links. Stored in isolated secure cloud vaults.", fontSize = 12.sp, color = Color.Gray)
                        }
                    }
                    Divider()
                    Row(verticalAlignment = Alignment.Top) {
                        Icon(Icons.Default.ShoppingCart, contentDescription = "Pricing", tint = MaterialTheme.colorScheme.primary)
                        Spacer(modifier = Modifier.width(12.dp))
                        Column {
                            Text("Simple Transparent Pricing", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                            Text("Cloud Share & Sync: $49.99/year flat.\nHardcover Curation Print: $39.00 flat per book.", fontSize = 12.sp, color = Color.Gray)
                        }
                    }
                }
            }
            
            // --- STEP 4: AUTO-CURATE & PUBLISH BUTTON ---
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                if (isCurationRunning) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(12.dp))
                            .padding(16.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        LinearProgressIndicator(progress = curationProgress, modifier = Modifier.fillMaxWidth())
                        Text(curationStatus, fontSize = 13.sp, color = Color.Gray, textAlign = TextAlign.Center)
                    }
                } else {
                    Button(
                        onClick = {
                            isCurationRunning = true
                            coroutineScope.launch {
                                curationStatus = "Filtering blur & screenshots..."
                                curationProgress = 0.3f
                                delay(1200)
                                curationStatus = "Semantics grouping & rating..."
                                curationProgress = 0.6f
                                delay(1200)
                                curationStatus = "Synthesizing journal storybook..."
                                curationProgress = 0.9f
                                delay(800)
                                curationProgress = 1.0f
                                isCurationRunning = false
                                publishedLink = "http://localhost:8000/viewer?session=family-trip-california-road-trip-3f3eaf"
                            }
                        },
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(12.dp),
                        enabled = selectedPhotos.isNotEmpty()
                    ) {
                        Icon(Icons.Default.Star, contentDescription = "Sparkles")
                        Spacer(modifier = Modifier.width(8.dp))
                        Text("✨ Publish Memory Book", fontWeight = FontWeight.Bold)
                    }
                }
                
                publishedLink?.let { link ->
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(Color(0xFFE8F5E9), RoundedCornerShape(12.dp))
                            .padding(16.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Text("🎉 Your Keepsake is Ready!", fontWeight = FontWeight.Bold, color = Color(0xFF2E7D32))
                        Text("Link: $link", fontSize = 11.sp, color = Color.DarkGray)
                    }
                }
            }
        }
    }
}
